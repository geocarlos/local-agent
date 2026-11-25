"""Agent loop tests against a fake Ollama client (no real model)."""

from fakes import text_reply, tool_reply

from local_agent import tools
from local_agent.config import (
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_MAX_TOOL_ROUNDS,
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    Settings,
)
from local_agent.core import loop
from local_agent.core.events import (
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    TurnDone,
    Usage,
)
from local_agent.permissions import Risk
from local_agent.tools import tool
from local_agent.tools.output import truncate


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
    assert client.calls[0]["model"] == DEFAULT_MODEL
    # The default profile (coding) offers every tool except the network one.
    assert client.calls[0]["tools"] == [t.schema() for t in tools.TOOLS.values() if t.name != "web_fetch"]
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "hello"
    assert not messages[-1].tool_calls
    assert kinds(events) == ["text_delta", "turn_done"]
    assert events[0].text == "hello"
    assert events[1].stop_reason == "end_turn"
    assert events[1].text == "hello"
    # The core never prints; the frontend renders events.
    assert capsys.readouterr().out == ""


def test_one_tool_call_round(fake_ollama, workspace):
    target = workspace / "notes.txt"
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


def test_read_file_error_is_returned_to_model(fake_ollama, workspace):
    missing = workspace / "missing.txt"
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

    assert len(client.calls) == DEFAULT_MAX_TOOL_ROUNDS
    assert sum(m["role"] == "tool" for m in messages) == DEFAULT_MAX_TOOL_ROUNDS
    done = events[-1]
    assert isinstance(done, TurnDone)
    assert done.stop_reason == "max_turn_requests"
    assert done.text is None
    assert done.rounds == DEFAULT_MAX_TOOL_ROUNDS


def test_max_tool_rounds_cap_comes_from_settings(fake_ollama):
    client = fake_ollama([tool_reply(("no_such_tool", {}))], repeat_last=True)

    assert loop.run_turn(start("x"), client=client, settings=Settings(max_tool_rounds=3)) is None
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
    limit = DEFAULT_MAX_OUTPUT_CHARS
    exact = "x" * limit
    assert truncate(exact, limit) == exact

    over = "x" * (limit + 1)
    assert truncate(over, limit) == "x" * limit + "\n...[truncated 1 chars]"


# --- settings, num_ctx, token usage, thinking ------------------------------------

def test_num_ctx_is_sent_with_every_chat_call(fake_ollama, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    client = fake_ollama([tool_reply(("read_file", {"path": str(target)})), text_reply("ok")])

    loop.run_turn(start("read"), client=client, settings=Settings(num_ctx=4321))

    assert [call["options"] for call in client.calls] == [{"num_ctx": 4321}, {"num_ctx": 4321}]


def test_default_num_ctx_is_sent_when_no_settings_are_given(fake_ollama):
    client = fake_ollama([text_reply("hi")])

    loop.run_turn(start("hi"), client=client)

    assert client.calls[0]["options"] == {"num_ctx": DEFAULT_NUM_CTX}


def test_model_and_host_come_from_settings(fake_ollama):
    client = fake_ollama([text_reply("hi")])

    loop.run_turn(start("hi"), client=client, settings=Settings(model="other:1b"))

    assert client.calls[0]["model"] == "other:1b"


def test_token_counts_come_from_the_final_chunk_and_are_summed(fake_ollama, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    first = tool_reply(("read_file", {"path": str(target)}))
    first.prompt_eval_count, first.eval_count = 100, 20
    second = text_reply("ok", prompt_eval_count=150, eval_count=30)
    client = fake_ollama([first, second])
    events: list = []

    loop.run_turn(start("read"), client=client, emit=events.append)

    done = events[-1]
    # prompt_tokens is the last request's prompt size (the context in use); completion tokens add up over rounds.
    assert done.usage == Usage(prompt_tokens=150, completion_tokens=50)


def test_usage_is_none_when_ollama_reports_no_counts(fake_ollama):
    client = fake_ollama([text_reply("hi")])
    events: list = []

    loop.run_turn(start("hi"), client=client, emit=events.append)

    assert events[-1].usage is None


def test_usage_is_reported_when_the_round_cap_is_hit(fake_ollama, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    looping = tool_reply(("read_file", {"path": str(target)}))
    looping.prompt_eval_count, looping.eval_count = 40, 5
    client = fake_ollama([looping], repeat_last=True)
    events: list = []

    loop.run_turn(start("loop"), client=client, emit=events.append, settings=Settings(max_tool_rounds=2))

    assert events[-1].stop_reason == "max_turn_requests"
    assert events[-1].usage == Usage(prompt_tokens=40, completion_tokens=10)


def test_thinking_is_emitted_as_an_event_and_kept_in_history(fake_ollama):
    client = fake_ollama([text_reply("the answer", thinking="let me reason")])
    events: list = []
    messages = start("hi")

    loop.run_turn(messages, client=client, emit=events.append)

    thinking = [e for e in events if isinstance(e, ThinkingDelta)]
    assert [e.text for e in thinking] == ["let me reason"]
    assert kinds(events)[0] == "thinking_delta"  # reasoning comes before the answer
    assert messages[-1].thinking == "let me reason"
    assert messages[-1].content == "the answer"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "the answer"


def test_settings_tool_limits_reach_the_tools(fake_ollama, workspace):
    target = workspace / "long.txt"
    target.write_text("x" * 50, encoding="utf-8")
    client = fake_ollama([tool_reply(("read_file", {"path": str(target)})), text_reply("ok")])
    messages = start("read")

    loop.run_turn(messages, client=client, settings=Settings(max_output_chars=10))

    tool_msg = next(m for m in messages if m["role"] == "tool")
    assert tool_msg["content"] == "x" * 10 + "\n...[truncated 40 chars]"
