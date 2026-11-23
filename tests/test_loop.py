"""Agent loop tests against a fake Ollama client (no real model)."""

from fakes import text_reply, tool_reply

from local_agent import tools
from local_agent.core import loop
from local_agent.tools import builtin
from local_agent.frontends import cli


def start(user_text: str) -> list:
    messages = loop.new_messages()
    messages.append({"role": "user", "content": user_text})
    return messages


def test_plain_text_reply(fake_ollama, capsys):
    client = fake_ollama([text_reply("hello")])
    messages = start("hi")

    assert loop.run_turn(messages, client=client) == "hello"

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == loop.MODEL
    assert client.calls[0]["tools"] == list(tools.TOOLS.values())
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "hello"
    assert not messages[-1].tool_calls
    assert "Agent: hello" in capsys.readouterr().out


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
    assert second_history[-1] == {"role": "tool", "content": "file body"}
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant"]


def test_tool_error_is_returned_to_model(fake_ollama, monkeypatch):
    def broken_tool(x: int) -> str:
        raise ValueError("boom")

    monkeypatch.setitem(tools.TOOLS, "broken_tool", broken_tool)
    client = fake_ollama([
        tool_reply(("broken_tool", {"x": 1}), ("no_such_tool", {})),
        text_reply("recovered"),
    ])
    messages = start("go")

    assert loop.run_turn(messages, client=client) == "recovered"

    tool_msgs = [m for m in client.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == (
        "Error calling broken_tool with arguments {'x': 1}: ValueError: boom"
    )
    assert tool_msgs[1]["content"] == "Unknown tool: no_such_tool"


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


def test_max_tool_rounds_cap(fake_ollama, tmp_path, capsys):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    client = fake_ollama([tool_reply(("read_file", {"path": str(target)}))], repeat_last=True)
    messages = start("loop forever")

    assert loop.run_turn(messages, client=client) is None

    assert len(client.calls) == loop.MAX_TOOL_ROUNDS
    assert sum(m["role"] == "tool" for m in messages) == loop.MAX_TOOL_ROUNDS
    assert f"stopped after {loop.MAX_TOOL_ROUNDS} tool rounds" in capsys.readouterr().out


def test_max_tool_rounds_cap_respects_patched_value(fake_ollama, monkeypatch):
    monkeypatch.setattr(loop, "MAX_TOOL_ROUNDS", 3)
    client = fake_ollama([tool_reply(("no_such_tool", {}))], repeat_last=True)

    assert loop.run_turn(start("x"), client=client) is None
    assert len(client.calls) == 3


def test_truncate_boundary():
    limit = builtin.MAX_OUTPUT_CHARS
    exact = "x" * limit
    assert builtin._truncate(exact) == exact

    over = "x" * (limit + 1)
    assert builtin._truncate(over) == "x" * limit + "\n...[truncated 1 chars]"


def test_cli_repl_runs_a_turn_and_exits(fake_ollama, monkeypatch, capsys):
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = fake_ollama([text_reply("hi there")])

    cli.run(client=client)

    out = capsys.readouterr().out
    assert f"Local Agent ready ({loop.MODEL})" in out
    assert "Agent: hi there" in out
    assert client.calls[0]["messages"][-1] == {"role": "user", "content": "hello"}
