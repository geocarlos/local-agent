"""Streaming, cancel and Ollama error handling in the loop (no real model).

The history invariant is asserted with assert_tool_calls_answered: every
assistant message with tool_calls is followed by one tool message per call.
"""

import threading

import httpx
import ollama
import pytest
from fakes import FakeOllama, Raise, Run, Stream, chunk, chunked, text_reply, tool_reply

from local_agent import tools
from local_agent.config import DEFAULT_MODEL, Settings
from local_agent.core import loop
from local_agent.core.events import Error, TextDelta, ToolCall, ToolResult, TurnDone
from local_agent.permissions import Risk
from local_agent.tools import tool


def start(user_text: str) -> list:
    messages = loop.new_messages()
    messages.append({"role": "user", "content": user_text})
    return messages


def make_tool(fn, risk=Risk.READ):
    table: dict = {}
    tool(risk=risk, registry=table)(fn)
    return table[fn.__name__]


def kinds(events) -> list[str]:
    return [e.type for e in events]


def deltas(events) -> list[str]:
    return [e.text for e in events if isinstance(e, TextDelta)]


def assert_tool_calls_answered(messages):
    """The history invariant: each assistant tool_calls message has one tool message per call, right after it."""
    for index, msg in enumerate(messages):
        calls = msg.get("tool_calls") if msg["role"] == "assistant" else None
        if not calls:
            continue
        end = index + 1
        while end < len(messages) and messages[end]["role"] == "tool":
            end += 1
        assert end - (index + 1) == len(calls), (
            f"assistant message {index} has {len(calls)} tool calls but {end - index - 1} tool results"
        )


class DownClient:
    """Raises the error the client gives when nothing listens on the port (non-streaming path)."""

    def chat(self, **kwargs):
        raise ConnectionError("Failed to connect to Ollama.")


# --- streaming -------------------------------------------------------------

def test_text_deltas_arrive_in_order_and_join_to_the_reply(fake_ollama):
    client = fake_ollama([text_reply("hello there, friend")], chunk_chars=4)
    events: list = []
    messages = start("hi")

    assert loop.run_turn(messages, client=client, emit=events.append) == "hello there, friend"

    texts = deltas(events)
    assert len(texts) > 1  # really streamed, not one delta
    assert "".join(texts) == "hello there, friend"
    assert kinds(events) == ["text_delta"] * len(texts) + ["turn_done"]
    assert events[-1].text == "hello there, friend"
    assert client.calls[0]["stream"] is True
    assert messages[-1].content == "hello there, friend"


def test_tool_calls_split_across_chunks_are_collected(fake_ollama, workspace):
    a, b = workspace / "a.txt", workspace / "b.txt"
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    # chunked() puts each tool call in its own chunk, so the two calls only meet in the accumulator.
    scripted = chunked(tool_reply(("read_file", {"path": str(a)}), ("read_file", {"path": str(b)})))
    client = fake_ollama([scripted, text_reply("both read")])
    events: list = []
    messages = start("read both")

    assert loop.run_turn(messages, client=client, emit=events.append) == "both read"

    calls = [e for e in events if isinstance(e, ToolCall)]
    assert [c.arguments["path"] for c in calls] == [str(a), str(b)]
    assert [r.content for r in events if isinstance(r, ToolResult)] == ["A", "B"]
    assert len(messages[2].tool_calls) == 2
    assert_tool_calls_answered(messages)


def test_tool_message_carries_tool_name(fake_ollama, workspace):
    target = workspace / "a.txt"
    target.write_text("x", encoding="utf-8")
    client = fake_ollama([tool_reply(("read_file", {"path": str(target)})), text_reply("ok")])
    messages = start("read")

    loop.run_turn(messages, client=client)

    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert tool_msgs == [{"role": "tool", "content": "x", "tool_name": "read_file"}]


# --- cancel ----------------------------------------------------------------

def test_cancel_mid_stream_drops_the_partial_reply(fake_ollama):
    cancel = threading.Event()
    client = FakeOllama([Stream([
        chunk(content="he"),
        chunk(content="llo"),
        Run(cancel.set),
        chunk(content=" world"),
        chunk(done=True),
    ])])
    events: list = []
    messages = start("hi")

    assert loop.run_turn(messages, client=client, emit=events.append, cancel=cancel) is None

    assert deltas(events) == ["he", "llo"]
    assert kinds(events)[-1] == "turn_done"
    assert events[-1].stop_reason == "cancelled"
    assert events[-1].text is None
    # The partial reply never reached history; the user message is last.
    assert [m["role"] for m in messages] == ["system", "user"]
    assert_tool_calls_answered(messages)


def test_cancel_before_the_turn_makes_no_model_call(fake_ollama):
    cancel = threading.Event()
    cancel.set()
    client = fake_ollama([text_reply("never")])
    events: list = []

    assert loop.run_turn(start("hi"), client=client, emit=events.append, cancel=cancel) is None

    assert client.calls == []
    assert events[-1].stop_reason == "cancelled"


def test_cancel_between_tool_calls_answers_every_call(fake_ollama, monkeypatch, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    cancel = threading.Event()

    def first_tool() -> str:
        """Runs, then asks for the turn to stop."""
        cancel.set()
        return "first ran"

    monkeypatch.setitem(tools.TOOLS, "first_tool", make_tool(first_tool))
    client = fake_ollama([
        tool_reply(("first_tool", {}), ("read_file", {"path": str(target)}), ("read_file", {"path": str(target)})),
        text_reply("should not be asked"),
    ])
    events: list = []
    messages = start("go")

    assert loop.run_turn(messages, client=client, emit=events.append, cancel=cancel) is None

    assert len(client.calls) == 1  # no further model call after the cancel
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.status for r in results] == ["completed", "failed", "failed"]
    assert results[1].content == loop.CANCELLED_MESSAGE
    assert [e.stop_reason for e in events if isinstance(e, TurnDone)] == ["cancelled"]
    assert [m["content"] for m in messages if m["role"] == "tool"] == [
        "first ran", loop.CANCELLED_MESSAGE, loop.CANCELLED_MESSAGE,
    ]
    assert_tool_calls_answered(messages)


def test_ctrl_c_inside_a_tool_cancels_the_rest(fake_ollama, monkeypatch, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")

    def interrupted() -> str:
        """Ctrl+C arrives while this tool runs."""
        raise KeyboardInterrupt

    monkeypatch.setitem(tools.TOOLS, "interrupted", make_tool(interrupted))
    client = fake_ollama([
        tool_reply(("interrupted", {}), ("read_file", {"path": str(target)})),
        text_reply("unused"),
    ])
    events: list = []
    messages = start("go")

    assert loop.run_turn(messages, client=client, emit=events.append) is None

    assert len(client.calls) == 1
    assert [r.status for r in events if isinstance(r, ToolResult)] == ["failed", "failed"]
    assert events[-1].stop_reason == "cancelled"
    assert_tool_calls_answered(messages)


def test_ctrl_c_at_approval_cancels_and_leaves_the_file_alone(fake_ollama, tmp_path):
    target = tmp_path / "out.txt"

    class InterruptingApprover:
        def decide(self, request):
            raise KeyboardInterrupt

    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("unused"),
    ])
    events: list = []
    messages = start("write")

    assert loop.run_turn(messages, client=client, emit=events.append, approver=InterruptingApprover()) is None

    assert not target.exists()
    assert len(client.calls) == 1
    assert events[-1].stop_reason == "cancelled"
    assert messages[-1] == {"role": "tool", "content": loop.CANCELLED_MESSAGE, "tool_name": "write_file"}
    assert_tool_calls_answered(messages)


def test_ctrl_c_while_events_are_emitted_keeps_history_valid(fake_ollama):
    def printer_that_is_interrupted(event):
        if isinstance(event, ToolCall):
            raise KeyboardInterrupt  # Ctrl+C between the model's reply and running its first call

    client = fake_ollama([tool_reply(("read_file", {"path": "x"}), ("read_file", {"path": "y"})), text_reply("no")])
    events: list = []

    def emit(event):
        printer_that_is_interrupted(event)
        events.append(event)

    messages = start("go")
    assert loop.run_turn(messages, client=client, emit=emit) is None

    assert_tool_calls_answered(messages)
    assert [m["content"] for m in messages if m["role"] == "tool"] == [loop.CANCELLED_MESSAGE] * 2
    assert events[-1].stop_reason == "cancelled"


def test_ctrl_c_while_reporting_the_cancel_is_contained(fake_ollama):
    cancel = threading.Event()
    cancel.set()

    def emit(event):
        if isinstance(event, TurnDone):
            raise KeyboardInterrupt  # a second Ctrl+C while the cancel is being reported

    messages = start("hi")
    client = fake_ollama([text_reply("unused")])

    assert loop.run_turn(messages, client=client, emit=emit, cancel=cancel) is None  # no traceback
    assert [m["role"] for m in messages] == ["system", "user"]


def test_chunk_with_both_text_and_tool_calls_keeps_both(fake_ollama, workspace):
    target = workspace / "a.txt"
    target.write_text("A", encoding="utf-8")
    call = tool_reply(("read_file", {"path": str(target)})).message.tool_calls[0]
    client = FakeOllama([Stream([chunk(content="Checking. ", tool_calls=[call]), chunk(done=True)]), text_reply("A it is")])
    events: list = []
    messages = start("read")

    assert loop.run_turn(messages, client=client, emit=events.append) == "A it is"

    assert deltas(events)[0] == "Checking. "
    assert [r.content for r in events if isinstance(r, ToolResult)] == ["A"]
    assert messages[2].content == "Checking. "
    assert len(messages[2].tool_calls) == 1
    assert_tool_calls_answered(messages)


def test_ctrl_c_during_stream_cancels(fake_ollama):
    client = FakeOllama([Stream([chunk(content="he"), Raise(KeyboardInterrupt())])])
    events: list = []
    messages = start("hi")

    assert loop.run_turn(messages, client=client, emit=events.append) is None

    assert deltas(events) == ["he"]
    assert events[-1].stop_reason == "cancelled"
    assert [m["role"] for m in messages] == ["system", "user"]


# --- Ollama errors ---------------------------------------------------------

@pytest.mark.parametrize("client", [DownClient(), "stream"], ids=["call-time", "stream-time"])
def test_ollama_down_emits_error_naming_the_host(client):
    settings = Settings(host="http://127.0.0.1:1")
    if client == "stream":
        client = FakeOllama([Stream([Raise(httpx.ConnectError("refused"))])])
    events: list = []
    messages = start("hi")

    assert loop.run_turn(messages, client=client, settings=settings, emit=events.append) is None

    errors = [e for e in events if isinstance(e, Error)]
    assert len(errors) == 1
    assert "Cannot reach Ollama at http://127.0.0.1:1" in errors[0].message
    assert "ollama serve" in errors[0].message
    assert not any(isinstance(e, TurnDone) for e in events)  # an Error ends the turn
    assert [m["role"] for m in messages] == ["system", "user"]


def test_ollama_down_with_the_real_client_names_the_port():
    client = ollama.Client(host="http://127.0.0.1:1")
    events: list = []

    assert loop.run_turn(start("hi"), client=client, settings=Settings(host="http://127.0.0.1:1"),
                         emit=events.append) is None

    message = events[-1].message
    assert isinstance(events[-1], Error)
    assert "http://127.0.0.1:1" in message
    assert "ollama serve" in message


def test_400_mentioning_not_found_is_not_a_missing_model(fake_ollama):
    client = FakeOllama([Stream([Raise(ollama.ResponseError("tool not found", 400))])])
    events: list = []

    assert loop.run_turn(start("hi"), client=client, emit=events.append) is None

    assert events[-1] == Error(message="Ollama returned an error: tool not found")


@pytest.mark.parametrize("error", [
    ollama.ResponseError('model "qwen3-vl:8b" not found', 404),  # HTTP 404 before the stream starts
    ollama.ResponseError("model 'qwen3-vl:8b' not found"),       # error object inside the stream
])
def test_model_not_found_suggests_ollama_pull(fake_ollama, error):
    client = FakeOllama([Stream([Raise(error)])])
    events: list = []

    assert loop.run_turn(start("hi"), client=client, emit=events.append) is None

    errors = [e for e in events if isinstance(e, Error)]
    assert len(errors) == 1
    assert f"ollama pull {DEFAULT_MODEL}" in errors[0].message
    assert not any(isinstance(e, TurnDone) for e in events)


def test_other_ollama_errors_are_shown(fake_ollama):
    client = FakeOllama([Stream([Raise(ollama.ResponseError("out of memory", 500))])])
    events: list = []

    assert loop.run_turn(start("hi"), client=client, emit=events.append) is None

    assert events[-1] == Error(message="Ollama returned an error: out of memory")


def test_error_in_a_later_round_leaves_history_valid(fake_ollama, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("a", encoding="utf-8")
    client = FakeOllama([
        tool_reply(("read_file", {"path": str(target)})),
        Stream([Raise(httpx.ConnectError("refused"))]),
    ])
    events: list = []
    messages = start("read")

    assert loop.run_turn(messages, client=client, emit=events.append) is None

    assert isinstance(events[-1], Error)
    assert messages[-1]["role"] == "tool"
    assert_tool_calls_answered(messages)


def test_unexpected_exceptions_are_not_hidden(fake_ollama):
    client = FakeOllama([Stream([Raise(ValueError("bug"))])])

    with pytest.raises(ValueError):
        loop.run_turn(start("hi"), client=client)
