"""Agent loop tests against a fake Ollama client (no real model)."""

from fakes import text_reply, tool_reply

from local_agent import tools
from local_agent.core import loop
from local_agent.core.events import ToolCall, ToolResult, TurnDone
from local_agent.permissions import Risk
from local_agent.tools import builtin, tool


def start(user_text: str) -> list:
    messages = loop.new_messages()
    messages.append({"role": "user", "content": user_text})
    return messages


def make_tool(fn, risk=Risk.READ):
    """Register `fn` in a private table and return its Tool (keeps TOOLS untouched)."""
    table: dict = {}
    tool(risk=risk, registry=table)(fn)
    return table[fn.__name__]


def kinds(events) -> list[str]:
    return [e.type for e in events]


def test_plain_text_reply(fake_ollama, capsys):
    client = fake_ollama([text_reply("hello")])
    messages = start("hi")
    events: list = []

    assert loop.run_turn(messages, client=client, emit=events.append) == "hello"

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == loop.MODEL
    assert client.calls[0]["tools"] == [t.schema() for t in tools.TOOLS.values()]
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "hello"
    assert not messages[-1].tool_calls
    assert kinds(events) == ["text_delta", "turn_done"]
    assert events[0].text == "hello"
    assert events[1].stop_reason == "end_turn"
    assert events[1].text == "hello"
    # The core never prints; the frontend renders events.
    assert capsys.readouterr().out == ""


def test_one_tool_call_round(fake_ollama, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("file body", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("read_file", {"path": str(target)})),
        text_reply("the file says: file body"),
    ])
    messages = start("read notes.txt")

    assert loop.run_turn(messages, client=client) == "the file says: file body"

    assert len(client.calls) == 2
    # The second model call saw the tool result.
    second_history = client.calls[1]["messages"]
    assert second_history[-2].tool_calls[0].function.name == "read_file"
    assert second_history[-1] == {"role": "tool", "content": "file body", "tool_name": "read_file"}
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant"]


def test_tool_error_is_returned_to_model(fake_ollama, monkeypatch):
    def broken_tool(x: int) -> str:
        """Always fails."""
        raise ValueError("boom")

    monkeypatch.setitem(tools.TOOLS, "broken_tool", make_tool(broken_tool))
    client = fake_ollama([
        tool_reply(("broken_tool", {"x": 1}), ("no_such_tool", {})),
        text_reply("recovered"),
    ])
    events: list = []
    messages = start("go")

    assert loop.run_turn(messages, client=client, emit=events.append) == "recovered"

    tool_msgs = [m for m in client.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == (
        "Error calling broken_tool with arguments {'x': 1}: ValueError: boom"
    )
    assert tool_msgs[1]["content"] == "Unknown tool: no_such_tool"
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.status for r in results] == ["failed", "failed"]


def test_read_file_error_is_returned_to_model(fake_ollama, tmp_path):
    missing = tmp_path / "missing.txt"
    client = fake_ollama([
        tool_reply(("read_file", {"path": str(missing)})),
        text_reply("that file does not exist"),
    ])
    messages = start("read missing.txt")

    loop.run_turn(messages, client=client)

    tool_msg = client.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"].startswith("Error: ")


def test_max_tool_rounds_cap(fake_ollama, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    client = fake_ollama([tool_reply(("read_file", {"path": str(target)}))], repeat_last=True)
    events: list = []
    messages = start("loop forever")

    assert loop.run_turn(messages, client=client, emit=events.append) is None

    assert len(client.calls) == loop.MAX_TOOL_ROUNDS
    assert sum(m["role"] == "tool" for m in messages) == loop.MAX_TOOL_ROUNDS
    done = events[-1]
    assert isinstance(done, TurnDone)
    assert done.stop_reason == "max_turn_requests"
    assert done.text is None
    assert done.rounds == loop.MAX_TOOL_ROUNDS


def test_max_tool_rounds_cap_respects_patched_value(fake_ollama, monkeypatch):
    monkeypatch.setattr(loop, "MAX_TOOL_ROUNDS", 3)
    client = fake_ollama([tool_reply(("no_such_tool", {}))], repeat_last=True)

    assert loop.run_turn(start("x"), client=client) is None
    assert len(client.calls) == 3


def test_tool_call_event_carries_kind_and_arguments(fake_ollama, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("read_file", {"path": str(target)})),
        text_reply("ok"),
    ])
    events: list = []

    loop.run_turn(start("read"), client=client, emit=events.append)

    call = next(e for e in events if isinstance(e, ToolCall))
    assert call.name == "read_file"
    assert call.kind == "read"
    assert call.arguments == {"path": str(target)}


def test_truncate_boundary():
    limit = builtin.MAX_OUTPUT_CHARS
    exact = "x" * limit
    assert builtin._truncate(exact) == exact

    over = "x" * (limit + 1)
    assert builtin._truncate(over) == "x" * limit + "\n...[truncated 1 chars]"
