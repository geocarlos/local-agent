"""Fallback parser: tool calls printed as text become real tool calls (or stay plain text)."""

import json

import pytest
from fakes import text_reply, tool_reply

from local_agent import tools
from local_agent.core import loop
from local_agent.core.events import ApprovalRequest, Decision, ToolCall, TurnDone
from local_agent.core.toolcalls import parse_text_tool_calls

READ = {"name": "read_file", "arguments": {"path": "a.txt"}}


def parse(content: str):
    return parse_text_tool_calls(content, tools.TOOLS)


def as_pairs(calls):
    return [(c["function"]["name"], dict(c["function"]["arguments"])) for c in calls]


# --- valid forms ----------------------------------------------------------------

def test_bare_json_object_is_a_call():
    assert as_pairs(parse(json.dumps(READ))) == [("read_file", {"path": "a.txt"})]


def test_json_in_a_json_fence_is_a_call():
    content = "```json\n" + json.dumps(READ, indent=2) + "\n```"

    assert as_pairs(parse(content)) == [("read_file", {"path": "a.txt"})]


def test_json_in_a_fence_without_a_language_is_a_call():
    assert as_pairs(parse("```\n" + json.dumps(READ) + "\n```")) == [("read_file", {"path": "a.txt"})]


def test_json_in_tool_call_tags_is_a_call():
    content = "<tool_call>\n" + json.dumps(READ) + "\n</tool_call>"

    assert as_pairs(parse(content)) == [("read_file", {"path": "a.txt"})]


def test_several_calls_in_one_array_or_several_blocks():
    other = {"name": "read_file", "arguments": {"path": "b.txt"}}
    assert as_pairs(parse(json.dumps([READ, other]))) == [
        ("read_file", {"path": "a.txt"}), ("read_file", {"path": "b.txt"}),
    ]
    assert as_pairs(parse(f"<tool_call>{json.dumps(READ)}</tool_call>\n<tool_call>{json.dumps(other)}</tool_call>")) == [
        ("read_file", {"path": "a.txt"}), ("read_file", {"path": "b.txt"}),
    ]


def test_arguments_given_as_a_json_string_are_decoded():
    content = json.dumps({"name": "read_file", "arguments": json.dumps({"path": "a.txt"})})

    assert as_pairs(parse(content)) == [("read_file", {"path": "a.txt"})]


def test_a_missing_arguments_key_is_empty_arguments_and_must_still_bind():
    # {"name": "read_file"} means arguments={}, which does not bind (path is required): plain text.
    assert parse(json.dumps({"name": "read_file"})) is None


def test_returned_calls_are_ollama_tool_calls():
    calls = parse(json.dumps(READ))

    assert calls[0].function.name == "read_file"
    assert calls[0].function.arguments == {"path": "a.txt"}


# --- rejected: treated as plain text ----------------------------------------------

@pytest.mark.parametrize("content", [
    json.dumps({"name": "no_such_tool", "arguments": {}}),                 # unknown tool
    json.dumps({"name": "read_file", "arguments": {"path": "a", "x": 1}}),  # extra argument
    json.dumps({"name": "read_file", "arguments": {}}),                    # missing required argument
    json.dumps({"name": "read_file", "arguments": ["a.txt"]}),             # arguments not an object
    json.dumps({"name": "read_file", "arguments": "not json"}),            # arguments string is not JSON
    json.dumps({"name": 7, "arguments": {}}),                              # name not a string
    json.dumps({"arguments": {"path": "a.txt"}}),                          # no name
    json.dumps({"answer": 42}),                                            # JSON, but not a call
    json.dumps(["read_file"]),                                             # array of non-calls
    json.dumps([READ, {"name": "no_such_tool"}]),                          # one good call, one bad: all or nothing
    "42",
    '"just a string"',
])
def test_bad_json_or_bad_calls_are_plain_text(content):
    assert parse(content) is None


@pytest.mark.parametrize("content", [
    "Use {x} to format the string.",
    'The config is {"name": "read_file", "arguments": {"path": "a"}} as an example.',
    "Here is a call:\n```json\n" + json.dumps(READ) + "\n```\nLet me know if it works.",
    "I will read the file now. " + json.dumps(READ),
    "```python\nprint('hi')\n```",
    "```json\n{not valid json\n```",
    "{unbalanced",
    "",
    "   ",
])
def test_prose_braces_and_broken_blocks_are_plain_text(content):
    assert parse(content) is None


# --- through the loop: approvals and the history invariant still apply -------------

def test_a_fenced_write_call_asks_for_approval_and_runs_after_yes(fake_ollama, workspace):
    target = workspace / "out.txt"
    call = {"name": "write_file", "arguments": {"path": str(target), "content": "hi"}}
    client = fake_ollama([text_reply("```json\n" + json.dumps(call) + "\n```"), text_reply("done")])
    events: list = []
    requests: list[ApprovalRequest] = []

    class Yes:
        def decide(self, request):
            requests.append(request)
            return Decision.ALLOW_ONCE

    messages = loop.new_messages()
    messages.append({"role": "user", "content": "write it"})
    assert loop.run_turn(messages, client=client, emit=events.append, approver=Yes()) == "done"

    assert target.read_text(encoding="utf-8") == "hi"
    assert [r.tool_name for r in requests] == ["write_file"]
    call_event = next(e for e in events if isinstance(e, ToolCall))
    assert call_event.name == "write_file"
    assert call_event.arguments == {"path": str(target), "content": "hi"}
    assert messages[2].tool_calls[0].function.name == "write_file"
    assert messages[3]["role"] == "tool"
    assert events[-1] == TurnDone(stop_reason="end_turn", text="done", rounds=2, usage=None)


def test_a_fenced_write_call_is_refused_without_an_approver(fake_ollama, workspace):
    target = workspace / "out.txt"
    call = {"name": "write_file", "arguments": {"path": str(target), "content": "hi"}}
    client = fake_ollama([text_reply(json.dumps(call)), text_reply("ok")])
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "write it"})

    loop.run_turn(messages, client=client)

    assert not target.exists()
    assert messages[3] == {"role": "tool", "content": "User denied file write.", "tool_name": "write_file"}


def test_a_read_call_in_text_runs_without_asking(fake_ollama, workspace):
    target = workspace / "a.txt"
    target.write_text("A", encoding="utf-8")
    client = fake_ollama([text_reply(json.dumps(READ | {"arguments": {"path": str(target)}})), text_reply("A")])
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "read"})

    assert loop.run_turn(messages, client=client) == "A"
    assert messages[3] == {"role": "tool", "content": "A", "tool_name": "read_file"}


def test_plain_text_with_braces_is_a_normal_reply(fake_ollama):
    client = fake_ollama([text_reply("Use {x} in the format string.")])
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "how?"})

    assert loop.run_turn(messages, client=client) == "Use {x} in the format string."
    assert not messages[-1].tool_calls


def test_structured_tool_calls_are_not_parsed_from_text(fake_ollama, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("A", encoding="utf-8")
    # A structured call with text that looks like another call: the structured one wins, text is kept.
    response = tool_reply(("read_file", {"path": str(target)}))
    response.message.content = json.dumps({"name": "no_such_tool", "arguments": {}})
    client = fake_ollama([response, text_reply("ok")])
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "read"})

    loop.run_turn(messages, client=client)

    assert [c.function.name for c in messages[2].tool_calls] == ["read_file"]
