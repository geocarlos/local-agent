"""Tool profiles and the network switch: what is offered to the model, and what the loop will run.

The loop tests use a fake client (no Ollama) and a recording approver, so they show
both the `tools=` list sent to the model and whether the user was asked.
"""

import json

from fakes import text_reply, tool_reply

from wildcat_code_agent import tools
from wildcat_code_agent.config import DEFAULT_TOOL_PROFILE, TOOL_PROFILES, Settings
from wildcat_code_agent.core import loop
from wildcat_code_agent.core.events import Decision, ToolResult
from wildcat_code_agent.tools import profiles

READ_TOOLS = {"read_file", "list_dir", "glob", "grep", "git_status", "git_diff", "git_log"}
CODING_TOOLS = READ_TOOLS | {"edit_file", "write_file", "git_commit", "run_command", "run_tests"}


class RecordingApprover:
    def __init__(self, decision: Decision = Decision.ALLOW_ONCE):
        self.decision = decision
        self.requests: list = []

    def decide(self, request):
        self.requests.append(request)
        return self.decision


def run(client, settings: Settings, approver=None) -> tuple[list, list]:
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "go"})
    loop.run_turn(messages, client=client, settings=settings, emit=events.append,
                  approver=approver or RecordingApprover())
    return messages, events


def offered_names(client) -> set[str]:
    return {t["function"]["name"] for t in client.calls[0]["tools"]}


def result_of(events) -> ToolResult:
    return next(e for e in events if isinstance(e, ToolResult))


# --- defaults and selection -----------------------------------------------------------------

def test_the_default_profile_is_coding_and_the_network_is_off():
    settings = Settings()

    assert DEFAULT_TOOL_PROFILE == "coding"
    assert settings.tool_profile == "coding"
    assert settings.network_enabled is False
    assert set(TOOL_PROFILES) == {"minimal", "coding", "all"}


def test_the_default_offers_the_coding_tools_and_not_web_fetch():
    offered = profiles.offered_tools(tools.TOOLS, Settings())

    assert set(offered) == CODING_TOOLS
    assert "web_fetch" not in offered


def test_minimal_offers_only_the_read_tools():
    offered = profiles.offered_tools(tools.TOOLS, Settings(tool_profile="minimal", network_enabled=True))

    assert set(offered) == READ_TOOLS


def test_all_without_the_network_switch_leaves_web_fetch_out():
    offered = profiles.offered_tools(tools.TOOLS, Settings(tool_profile="all", network_enabled=False))

    assert set(offered) == CODING_TOOLS
    assert "web_fetch" not in offered


def test_all_with_the_network_switch_offers_everything_including_web_fetch():
    offered = profiles.offered_tools(tools.TOOLS, Settings(tool_profile="all", network_enabled=True))

    assert set(offered) == set(tools.TOOLS)
    assert "web_fetch" in offered


def test_the_network_switch_alone_does_not_add_web_fetch_to_the_coding_profile():
    offered = profiles.offered_tools(tools.TOOLS, Settings(tool_profile="coding", network_enabled=True))

    assert "web_fetch" not in offered


def test_every_profile_name_has_a_risk_set():
    assert set(profiles.PROFILE_RISKS) == set(TOOL_PROFILES)


def test_web_fetch_is_in_the_network_tier_and_always_asks_without_allow_always():
    tool = tools.TOOLS["web_fetch"]

    assert tool.risk.value == "network"
    assert tool.allow_always is False


# --- the model is not offered tools outside the profile ----------------------------------------

def test_the_tools_list_sent_to_the_model_follows_the_profile(fake_ollama):
    client = fake_ollama([text_reply("hi")])

    run(client, Settings(tool_profile="minimal"))

    assert offered_names(client) == READ_TOOLS


def test_a_disabled_web_fetch_is_absent_from_tools_even_under_all(fake_ollama):
    client = fake_ollama([text_reply("hi")])

    run(client, Settings(tool_profile="all", network_enabled=False))

    assert "web_fetch" not in offered_names(client)


def test_a_disabled_web_fetch_called_anyway_is_refused_without_asking(fake_ollama):
    client = fake_ollama([
        tool_reply(("web_fetch", {"url": "https://example.com/"})),
        text_reply("ok"),
    ])
    approver = RecordingApprover()

    messages, events = run(client, Settings(tool_profile="all", network_enabled=False), approver)

    assert approver.requests == []
    result = result_of(events)
    assert result.status == "failed"
    assert result.content == ("Error: web_fetch is disabled; enable it in config "
                              "(network_enabled = true in config.toml).")
    assert messages[-2] == {"role": "tool", "content": result.content, "tool_name": "web_fetch"}


def test_a_disabled_web_fetch_written_as_text_is_refused_the_same_way(fake_ollama):
    call = json.dumps({"name": "web_fetch", "arguments": {"url": "https://example.com/"}})
    client = fake_ollama([text_reply(call), text_reply("ok")])
    approver = RecordingApprover()

    _, events = run(client, Settings(tool_profile="all", network_enabled=False), approver)

    assert approver.requests == []
    assert result_of(events).content.startswith("Error: web_fetch is disabled; enable it in config")


def test_a_call_outside_the_profile_is_refused_without_asking(fake_ollama, workspace):
    target = workspace / "out.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("ok"),
    ])
    approver = RecordingApprover()

    _, events = run(client, Settings(tool_profile="minimal"), approver)

    assert approver.requests == []
    assert not target.exists()
    result = result_of(events)
    assert result.status == "failed"
    assert result.content == ("Error: write_file is not in the 'minimal' tool profile. "
                              "Use another profile (tool_profile in config.toml, or --profile).")


def test_a_read_tool_still_runs_under_minimal(fake_ollama, workspace):
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("read_file", {"path": "a.txt"})),
        text_reply("ok"),
    ])
    approver = RecordingApprover()

    _, events = run(client, Settings(tool_profile="minimal"), approver)

    assert approver.requests == []
    assert result_of(events).content == "hello\n"


def test_an_unknown_tool_is_still_reported_as_unknown(fake_ollama):
    client = fake_ollama([tool_reply(("no_such_tool", {})), text_reply("ok")])

    _, events = run(client, Settings(tool_profile="all", network_enabled=True))

    assert result_of(events).content == "Unknown tool: no_such_tool"


# --- tool descriptions stay short (small models) -----------------------------------------------

def test_tool_descriptions_stay_short():
    # Pins the length budget: one or two sentences, and at most 200 characters for each tool.
    for name, tool in tools.TOOLS.items():
        sentences = [s for s in tool.description.replace("\n", " ").split(". ") if s.strip()]
        assert len(tool.description) <= 200, name
        assert len(sentences) <= 2, name


def test_the_coding_profile_schemas_stay_within_budget():
    # The schemas of the tools a default session offers are sent with every request. They were 5319
    # characters when this budget was set (5941 before T08 trimmed them); the budget catches growth.
    offered = profiles.offered_tools(tools.TOOLS, Settings())
    size = sum(len(json.dumps(tool.schema())) for tool in offered.values())

    assert size <= 5400, size


def test_edit_file_says_old_text_must_match_once():
    # The model has to include enough context for old_text to be unique; the description says so.
    description = tools.TOOLS["edit_file"].description
    assert "unique" in description and "surrounding lines" in description
