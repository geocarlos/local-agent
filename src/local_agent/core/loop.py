"""The agent loop: one user turn of model <-> tool rounds.

The loop reports what happens as events (core/events.py) through an EventSink,
and asks an Approver before any write, exec or network tool runs. It never
prints or reads input; the frontend decides how to show events and answer.

Streaming: each model reply is requested with stream=True. Text is emitted as
TextDelta events while it arrives; tool calls are collected from any chunk.
The assistant message goes into history only once its stream has finished.

Cancelling: a turn stops when `cancel` is set (checked between chunks, before
each round and before each tool call) or when KeyboardInterrupt arrives (how a
terminal delivers Ctrl+C). Either way the turn ends with TurnDone(cancelled).

History invariant: after every run_turn, each assistant message with tool_calls
is followed by exactly one tool message per call. Calls that did not run are
answered with CANCELLED_MESSAGE. A reply cut off mid-stream is dropped, so it
never reaches history.
"""

import os
import threading
from typing import Any, Protocol

import httpx
import ollama

from local_agent import tools
from local_agent.core.events import (
    Error,
    EventSink,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolStatus,
    TurnDone,
    discard,
)
from local_agent.permissions import Approver, DenyAll, needs_approval
from local_agent.tools import Location, Tool

MODEL = "qwen3-vl:8b"  # Swap for gemma4:12b, qwen2.5-coder:7b, etc.
MAX_TOOL_ROUNDS = 10         # Max model<->tool rounds per user turn
SYSTEM_PROMPT = "You are a concise local coding assistant. Use tools to inspect and edit files."
CANCELLED_MESSAGE = "Cancelled by user."
DEFAULT_HOST = "http://127.0.0.1:11434"


class ChatClient(Protocol):
    """The part of the Ollama client the loop uses. The `ollama` module itself fits."""

    def chat(self, *, model: str, messages: list, tools: list, stream: bool = False) -> Any: ...


def new_messages() -> list:
    """Fresh conversation history with the system prompt."""
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def run_turn(
    messages: list,
    client: ChatClient = ollama,
    *,
    emit: EventSink = discard,
    approver: Approver | None = None,
    cancel: threading.Event | None = None,
) -> str | None:
    """Run one user turn. `messages` must already end with the user message.

    Appends the model replies and tool results to `messages`, and emits events
    to `emit`. Write, exec and network tools are approved by `approver`; without
    one, they are refused. Set `cancel` (or press Ctrl+C) to stop the turn early.
    A `cancel` event passed in is not cleared by the loop: once set, it stays set
    for the caller to reset before reusing it. Returns the final reply text, or
    None if the turn hit MAX_TOOL_ROUNDS, was cancelled, or failed (an Error
    event is emitted for failures).
    """
    approver = approver or DenyAll()
    cancel = cancel if cancel is not None else threading.Event()
    try:
        return _run_rounds(messages, client, emit, approver, cancel)
    except KeyboardInterrupt:
        # Ctrl+C outside the guarded steps (for example while an event is being printed).
        # Keep the invariant even so, then end the turn as cancelled.
        cancel.set()
        _answer_open_calls(messages)
        try:
            emit(TurnDone(stop_reason="cancelled", text=None))
        except KeyboardInterrupt:  # a second Ctrl+C while the cancel is reported: the history is already valid
            pass
        return None


def _run_rounds(
    messages: list,
    client: ChatClient,
    emit: EventSink,
    approver: Approver,
    cancel: threading.Event,
) -> str | None:
    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        if cancel.is_set():
            emit(TurnDone(stop_reason="cancelled", text=None, rounds=round_no - 1))
            return None

        try:
            msg = _stream_reply(client, messages, emit, cancel)
        except KeyboardInterrupt:
            cancel.set()
            msg = None
        except (ConnectionError, httpx.TransportError, ollama.ResponseError) as e:
            emit(Error(message=_describe_model_error(e, client)))
            return None
        if msg is None:  # cancelled while streaming: the partial reply is dropped
            emit(TurnDone(stop_reason="cancelled", text=None, rounds=round_no))
            return None
        messages.append(msg)

        # No tool calls: the reply is the final answer for this turn.
        calls = msg.get("tool_calls")
        if not calls:
            text = msg.get("content") or ""
            emit(TurnDone(stop_reason="end_turn", text=text, rounds=round_no))
            return text

        # Execute each requested tool and feed results back to the model.
        if _run_calls(calls, round_no, messages, emit, approver, cancel):
            emit(TurnDone(stop_reason="cancelled", text=None, rounds=round_no))
            return None

    emit(TurnDone(stop_reason="max_turn_requests", text=None, rounds=MAX_TOOL_ROUNDS))
    return None


def _stream_reply(client: ChatClient, messages: list, emit: EventSink, cancel: threading.Event):
    """Stream one model reply, emitting its text. Returns the complete assistant message.

    Returns None if cancelled before the stream finished. Connection and model
    errors propagate to run_turn.
    """
    parts: list[str] = []
    tool_calls: list = []
    stream = client.chat(model=MODEL, messages=messages, tools=_tool_schemas(), stream=True)
    try:
        for chunk in stream:
            if cancel.is_set():
                return None
            part = chunk["message"]
            text = part.get("content") or ""
            if text:
                parts.append(text)
                emit(TextDelta(text=text))
            # Tool calls may arrive in any chunk, so collect them across the whole stream.
            tool_calls.extend(part.get("tool_calls") or [])
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    return ollama.Message(role="assistant", content="".join(parts), tool_calls=tool_calls or None)


def _run_calls(
    calls: list,
    round_no: int,
    messages: list,
    emit: EventSink,
    approver: Approver,
    cancel: threading.Event,
) -> bool:
    """Run the tool calls of one reply. Returns True if the turn was cancelled.

    Every call gets a tool message, even after a cancel: calls that did not run
    get CANCELLED_MESSAGE as a failed result.
    """
    for index, call in enumerate(calls, start=1):
        name = call["function"]["name"]
        args = dict(call["function"]["arguments"] or {})
        call_id = f"{round_no}.{index}"
        tool = tools.TOOLS.get(name)
        emit(ToolCall(tool_call_id=call_id, name=name, arguments=args,
                      kind=tool.kind if tool else "other"))
        if cancel.is_set():
            content, status = CANCELLED_MESSAGE, "failed"
        else:
            try:
                content, status = _run_tool(tool, name, call_id, args, emit, approver)
            except KeyboardInterrupt:  # Ctrl+C at an approval prompt or while a tool runs
                cancel.set()
                content, status = CANCELLED_MESSAGE, "failed"
        emit(ToolResult(tool_call_id=call_id, name=name, content=content, status=status))
        messages.append({"role": "tool", "content": content, "tool_name": name})
    return cancel.is_set()


def _answer_open_calls(messages: list) -> None:
    """Append CANCELLED_MESSAGE results for any tool calls in the last assistant message that have none."""
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if msg["role"] == "assistant":
            calls = msg.get("tool_calls") or []
            answered = 0
            for later in messages[index + 1:]:
                if later["role"] != "tool":
                    break
                answered += 1
            for call in calls[answered:]:
                messages.append({"role": "tool", "content": CANCELLED_MESSAGE,
                                 "tool_name": call["function"]["name"]})
            return


def _tool_schemas() -> list:
    return [t.schema() for t in tools.TOOLS.values()]


def _host_of(client: Any) -> str:
    """The Ollama host the client talks to, for error messages."""
    # The `ollama` module's client keeps its httpx client's base URL here.
    base_url = getattr(getattr(getattr(client, "_client", None), "_client", None), "base_url", None)
    return str(base_url).rstrip("/") if base_url else os.environ.get("OLLAMA_HOST", DEFAULT_HOST)


def _describe_model_error(error: BaseException, client: Any) -> str:
    """A message for the user about a failed model request."""
    if isinstance(error, (ConnectionError, httpx.ConnectError)):
        return (f"Cannot reach Ollama at {_host_of(client)}. "
                "Start it with `ollama serve` (or check OLLAMA_HOST), then try again.")
    if isinstance(error, ollama.ResponseError):
        text = str(error.error) if error.error else str(error)
        # 404 is "model not found" on the chat endpoint. -1 is an error object inside the stream, which has no status.
        missing = error.status_code == 404 or (error.status_code == -1 and "not found" in text.lower())
        if missing:
            return f"Model '{MODEL}' is not installed in Ollama. Run `ollama pull {MODEL}`, then try again."
        return f"Ollama returned an error: {text}"
    return f"Lost the connection to Ollama at {_host_of(client)}: {error}"


def _run_tool(
    tool: Tool | None,
    name: str,
    call_id: str,
    args: dict,
    emit: EventSink,
    approver: Approver,
) -> tuple[str, ToolStatus]:
    """Run one tool call after the approval policy. Failures come back as text for the model."""
    if tool is None:
        return f"Unknown tool: {name}", "failed"
    if tool.location is not Location.CORE:
        return f"Tool {name} runs on the client, and no client is connected.", "failed"
    try:
        # Bind first: bad arguments are reported before the user is asked, and with the real tool's name.
        tool.bind(args)
        if needs_approval(tool.risk):
            request = tool.approval_request(call_id, args)
            emit(request)
            try:
                decision = approver.decide(request)
            except Exception:
                # An approver that fails has not approved anything: refuse, and do not run the tool.
                return tool.denied_message, "failed"
            if not decision.allowed:
                return tool.denied_message, "failed"
        return tool.call(args), "completed"
    except Exception as e:
        return f"Error calling {name} with arguments {args}: {type(e).__name__}: {e}", "failed"
