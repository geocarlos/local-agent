"""MCP client against real stdio servers (tests/mcp_servers): listing, calls, errors, timeouts, cancel, shutdown.

The module-level runtime starts several servers once, which takes a few seconds. Tests
that end a server (a crash, a shutdown check) start their own.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fakes import FakeOllama, text_reply, tool_reply

from local_agent.config import McpServer, Settings
from local_agent.core import loop
from local_agent.core.events import Decision, TurnDone
from local_agent.permissions import Risk
from local_agent.tools import profiles
from local_agent.tools.mcp_tools import start_mcp_servers
from local_agent.tools.registry import REGISTRY, ToolContext
from test_streaming import assert_tool_calls_answered

SERVERS_DIR = Path(__file__).parent / "mcp_servers"
ECHO = str(SERVERS_DIR / "echo_server.py")
LEGACY = str(SERVERS_DIR / "legacy_server.py")
START_TIMEOUT_S = 20


def echo_server(**options) -> McpServer:
    return McpServer(name=options.pop("name"), command=sys.executable, args=(ECHO,), **options)


class Approver:
    """Records every approval request and answers with a fixed decision."""

    def __init__(self, decision: Decision = Decision.ALLOW_ONCE) -> None:
        self.decision = decision
        self.requests: list = []

    def decide(self, request) -> Decision:
        self.requests.append(request)
        return self.decision


def tool_content(messages: list, name: str) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "tool" and m.get("tool_name") == name]


def turn(replies: list, *, user: str, approver: Approver, settings: Settings | None = None,
         cancel: threading.Event | None = None):
    """Run one turn against a fake model. Returns (messages, fake, events)."""
    fake = FakeOllama(replies)
    messages = loop.new_messages()
    messages.append({"role": "user", "content": user})
    events: list = []
    loop.run_turn(messages, client=fake, settings=settings or Settings(), emit=events.append,
                  approver=approver, cancel=cancel)
    return messages, fake, events


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":  # os.kill(pid, 0) means CTRL_C_EVENT on Windows, so ask tasklist
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.fixture(scope="module")
def runtime():
    servers = {
        "plain": echo_server(name="plain"),
        "reader": echo_server(name="reader", risk="read"),
        "mixed": echo_server(name="mixed", tool_risks={"echo": "read", "nosuch": "read"}),
        "off": echo_server(name="off", enabled=False),
        "legacy": McpServer(name="legacy", command=sys.executable, args=(LEGACY,)),
        "broken": McpServer(name="broken", command=sys.executable,
                            args=("-c", "import sys; sys.stderr.write('no such thing'); sys.exit(1)")),
    }
    started = start_mcp_servers(servers, timeout_s=START_TIMEOUT_S)
    yield started
    started.close()


def test_the_tools_of_each_started_server_are_registered_with_the_server_schema(runtime):
    assert {"plain__echo", "plain__add", "plain__fail", "plain__picture", "reader__echo",
            "mixed__echo", "mixed__add", "legacy__shout"} <= set(REGISTRY)
    assert not any(name.startswith("off__") for name in REGISTRY)
    add = REGISTRY["plain__add"]
    assert add.parameters["type"] == "object"
    assert add.parameters["properties"]["a"]["type"] == "integer"
    assert add.parameters["required"] == ["a", "b"]
    schema = add.schema()
    assert schema["function"]["name"] == "plain__add"
    assert schema["function"]["parameters"] == add.parameters


def test_the_default_risk_is_exec_and_server_and_tool_settings_override_it(runtime):
    assert REGISTRY["plain__echo"].risk is Risk.EXEC
    assert REGISTRY["reader__echo"].risk is Risk.READ  # server-level risk
    assert REGISTRY["mixed__echo"].risk is Risk.READ   # per-tool override
    assert REGISTRY["mixed__add"].risk is Risk.EXEC    # the rest of the server keeps the default


def test_a_server_that_fails_to_start_gives_a_warning_and_the_others_still_work(runtime):
    (warning,) = [w for w in runtime.warnings if "'broken'" in w]
    assert "did not start" in warning and "no such thing" in warning
    assert "continues without" in warning
    assert "legacy__shout" in REGISTRY  # the agent still has the other servers
    assert any("'mixed'" in w and "nosuch" in w for w in runtime.warnings)


def test_a_server_that_speaks_only_the_older_handshake_is_listed_and_called(runtime):
    assert REGISTRY["legacy__shout"].call({"text": "hi"}, ToolContext()) == "HI"


def test_the_profiles_filter_mcp_tools_by_risk(runtime):
    coding = profiles.offered_tools(REGISTRY, Settings())
    minimal = profiles.offered_tools(REGISTRY, Settings(tool_profile="minimal"))
    assert "plain__add" in coding and "plain__add" not in minimal   # exec: coding only
    assert "reader__echo" in coding and "reader__echo" in minimal   # read: every profile


def test_an_exec_tool_is_offered_asked_and_its_result_reaches_the_model(runtime):
    approver = Approver(Decision.ALLOW_ONCE)
    messages, fake, events = turn([tool_reply(("plain__add", {"a": 2, "b": 3})), text_reply("the sum is 5")],
                                  user="add", approver=approver)

    offered = {t["function"]["name"]: t for t in fake.calls[0]["tools"]}
    assert offered["plain__add"]["function"]["parameters"] == REGISTRY["plain__add"].parameters

    (request,) = approver.requests
    assert request.tool_name == "plain__add" and request.risk == "exec"
    assert "Server: plain" in request.preview.text and "Tool: add" in request.preview.text
    assert '"a": 2' in request.preview.text and '"b": 3' in request.preview.text
    assert "allow_always" in request.options  # exec tools may be allowed for the session, like the others

    assert tool_content(fake.calls[1]["messages"], "plain__add") == ["5"]
    assert tool_content(messages, "plain__add") == ["5"]
    assert_tool_calls_answered(messages)


def test_an_exec_tool_refused_is_not_run(runtime):
    messages, fake, _ = turn([tool_reply(("plain__add", {"a": 1, "b": 1})), text_reply("ok")],
                             user="add", approver=Approver(Decision.REJECT_ONCE))
    assert tool_content(messages, "plain__add") == ["User denied plain__add."]
    assert_tool_calls_answered(messages)


def test_a_read_tier_tool_runs_without_asking(runtime):
    approver = Approver()
    messages, fake, _ = turn([tool_reply(("reader__echo", {"text": "quiet"})), text_reply("ok")],
                             user="echo", approver=approver)
    assert approver.requests == []
    assert tool_content(messages, "reader__echo") == ["quiet"]


def test_a_per_tool_read_override_runs_without_asking_while_the_rest_of_the_server_asks(runtime):
    approver = Approver()
    messages, _, _ = turn([tool_reply(("mixed__echo", {"text": "a"})), tool_reply(("mixed__add", {"a": 1, "b": 2})),
                           text_reply("done")], user="both", approver=approver)
    assert [r.tool_name for r in approver.requests] == ["mixed__add"]
    assert tool_content(messages, "mixed__echo") == ["a"]
    assert tool_content(messages, "mixed__add") == ["3"]


def test_an_error_result_from_the_server_comes_back_as_error_text(runtime):
    messages, _, _ = turn([tool_reply(("plain__fail", {})), text_reply("it failed")],
                          user="fail", approver=Approver())
    (content,) = tool_content(messages, "plain__fail")
    assert content.startswith("Error: ") and "fail" in content


def test_non_text_content_is_summarised(runtime):
    text = REGISTRY["plain__picture"].call({}, ToolContext())
    assert text.startswith("a picture\n[image: image/png, about ")


def test_a_call_past_its_time_limit_is_cancelled_and_says_so(runtime):
    messages, fake, events = turn([tool_reply(("plain__slow", {"seconds": 2})), text_reply("ok")],
                                  user="slow", approver=Approver(), settings=Settings(command_timeout_s=1))
    (content,) = tool_content(messages, "plain__slow")
    assert content == "Error: plain__slow did not complete: timed out after 1 s and was cancelled."
    assert_tool_calls_answered(messages)


def test_cancel_during_a_call_gives_cancelled_by_user_and_the_turn_ends(runtime):
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    try:
        messages, fake, events = turn([tool_reply(("plain__slow", {"seconds": 2})), text_reply("never")],
                                      user="slow", approver=Approver(), settings=Settings(command_timeout_s=30),
                                      cancel=cancel)
    finally:
        timer.cancel()
    assert tool_content(messages, "plain__slow") == ["Cancelled by user."]
    assert len(fake.calls) == 1  # no second model request after the cancel
    assert [e.stop_reason for e in events if isinstance(e, TurnDone)] == ["cancelled"]
    assert_tool_calls_answered(messages)


def test_a_server_that_exits_mid_session_gives_error_text_not_a_crash():
    started = start_mcp_servers({"dying": echo_server(name="dying")}, timeout_s=START_TIMEOUT_S)
    try:
        first = REGISTRY["dying__die"].call({}, ToolContext())
        assert first.startswith("Error: dying__die did not complete: the server failed (")
        later = REGISTRY["dying__add"].call({"a": 1, "b": 2}, ToolContext())
        assert later.startswith("Error: dying__add did not complete:")
    finally:
        started.close()
    assert "dying__add" not in REGISTRY


def test_closing_stops_the_server_process_and_removes_its_tools():
    started = start_mcp_servers({"pidsrv": echo_server(name="pidsrv")}, timeout_s=START_TIMEOUT_S)
    pid = int(REGISTRY["pidsrv__pid"].call({}, ToolContext()))
    assert process_alive(pid)
    started.close()
    assert not process_alive(pid)
    assert "pidsrv__pid" not in REGISTRY


def test_a_server_that_does_not_answer_in_time_is_stopped_and_skipped():
    sleeper = McpServer(name="sleepy", command=sys.executable, args=("-c", "import time; time.sleep(60)"))
    began = time.monotonic()
    started = start_mcp_servers({"sleepy": sleeper}, timeout_s=1)
    try:
        assert started.warnings and "'sleepy' did not start (did not answer within 1 s)" in started.warnings[0]
        assert not any(name.startswith("sleepy__") for name in REGISTRY)
    finally:
        started.close()
    assert time.monotonic() - began < 15


def test_a_server_sees_only_the_safe_environment_and_the_variables_set_in_its_env(monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_TEST_SECRET", "hunter2")
    monkeypatch.setenv("LOCAL_AGENT_TEST_PYTHONPATH", "/somewhere")
    servers = {"envsrv": echo_server(name="envsrv", env={"LOCAL_AGENT_TEST_SET": "yes"})}
    started = start_mcp_servers(servers, timeout_s=START_TIMEOUT_S)
    try:
        def seen(variable: str) -> str:
            return REGISTRY["envsrv__getenv"].call({"name": variable}, ToolContext())

        assert seen("LOCAL_AGENT_TEST_SECRET") == "(unset)"   # in the agent's environment, not in env
        assert seen("LOCAL_AGENT_TEST_PYTHONPATH") == "(unset)"
        assert seen("LOCAL_AGENT_TEST_SET") == "yes"          # set in env
        assert seen("PATH") == os.environ["PATH"]             # part of the safe subset
    finally:
        started.close()


def test_a_server_that_writes_a_lot_to_stderr_gives_a_short_escaped_warning():
    noisy = McpServer(name="noisy", command=sys.executable, args=(
        "-c", "import sys; sys.stderr.write('x' * 3000000 + '\\x1b[2JTHE-END\\n'); sys.exit(1)"))
    started = start_mcp_servers({"noisy": noisy}, timeout_s=START_TIMEOUT_S)
    try:
        (warning,) = started.warnings
        assert "THE-END" in warning                      # the end of the output is what is shown
        assert len(warning) < 1500                       # not the whole file
        assert "\x1b" not in warning and "\n" not in warning
        assert "\\x1b[2J" in warning                     # the escape is visible
    finally:
        started.close()
