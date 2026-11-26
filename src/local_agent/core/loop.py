"""The agent loop: one user turn of model <-> tool rounds.

The loop reports what happens as events (core/events.py) through an EventSink,
and asks an Approver before any write, exec or network tool runs, and before a
path argument that resolves outside the workspace is used (see sandbox.py). Only the
tools of the active profile are offered, and a call to any other tool is refused
(see tools/profiles.py). It never
prints or reads input; the frontend decides how to show events and answer.

Settings (config.Settings) are passed in: model, host, num_ctx (sent with every
chat call as options={"num_ctx": ...}), the round cap and the tool limits.

Streaming: each model reply is requested with stream=True. Text is emitted as
TextDelta events while it arrives; tool calls are collected from any chunk.
The assistant message goes into history only once its stream has finished.
If a reply has no structured tool calls, core/toolcalls.py may read calls
printed as text out of it.

Cancelling: a turn stops when `cancel` is set (checked between chunks, before
each round and before each tool call) or when KeyboardInterrupt arrives (how a
terminal delivers Ctrl+C). Either way the turn ends with TurnDone(cancelled).

History invariant: after every run_turn, each assistant message with tool_calls
is followed by exactly one tool message per call. Calls that did not run are
answered with CANCELLED_MESSAGE. A reply cut off mid-stream is dropped, so it
never reaches history.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import ollama

from local_agent import sandbox, tools
from local_agent.client_tools import ClientTools
from local_agent.config import Settings, plugin_dir_of
from local_agent.core.events import (
    ApprovalRequest,
    Decision,
    Error,
    EventSink,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    ToolStarted,
    ToolStatus,
    TurnDone,
    Usage,
    discard,
)
from local_agent.core.model import OLLAMA_ERRORS, describe_error
from local_agent.core.toolcalls import parse_text_tool_calls
from local_agent.permissions import Approver, DenyAll, SessionApprovals, needs_approval
from local_agent.tools import Location, Tool, profiles
from local_agent.tools.registry import ToolCancelled, ToolContext

# Small models (qwen2.5-coder:7b) answered "Say hi to X" with run_command `echo` under the old one-line prompt
# ("Use tools to inspect and edit files."). This wording keeps plain answers plain and still picks the right tool.
SYSTEM_PROMPT = (
    "You are a concise local coding assistant working in the user's project folder. "
    "Reply in plain text when you can answer directly, such as greetings, explanations or code shown in the reply. "
    "Call a tool only when you need to read, search or change files, or run a program the user asked for. "
    "Never use run_command just to print or echo a message."
)
CANCELLED_MESSAGE = "Cancelled by user."


class ChatClient(Protocol):
    """The part of the Ollama client the loop uses. The `ollama` module itself fits."""

    def chat(self, *, model: str, messages: list, tools: list, stream: bool = False,
             options: dict | None = None) -> Any: ...


def new_messages() -> list:
    """Fresh conversation history with the system prompt."""
    return [{"role": "system", "content": SYSTEM_PROMPT}]


@dataclass
class _Usage:
    """Token counts summed over the rounds of one turn."""

    prompt_tokens: int | None = None      # the last model request's prompt size
    completion_tokens: int | None = None  # generated tokens, all rounds

    def add(self, prompt_tokens: int | None, completion_tokens: int | None) -> None:
        if prompt_tokens is not None:
            self.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            self.completion_tokens = (self.completion_tokens or 0) + completion_tokens

    def value(self) -> Usage | None:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return Usage(prompt_tokens=self.prompt_tokens or 0, completion_tokens=self.completion_tokens or 0)


def run_turn(
    messages: list,
    client: ChatClient | None = None,
    *,
    settings: Settings | None = None,
    emit: EventSink = discard,
    approver: Approver | None = None,
    cancel: threading.Event | None = None,
    session: SessionApprovals | None = None,
    client_tools: ClientTools | None = None,
) -> str | None:
    """Run one user turn. `messages` must already end with the user message.

    Appends the model replies and tool results to `messages`, and emits events
    to `emit`. Write, exec and network tools are approved by `approver`; without
    one, they are refused. Set `cancel` (or press Ctrl+C) to stop the turn early.
    A `cancel` event passed in is not cleared by the loop: once set, it stays set
    for the caller to reset before reusing it. `client=None` builds an Ollama
    client for `settings.host`. Returns the final reply text, or None if the
    turn hit the round cap, was cancelled, or failed (an Error event is emitted
    for failures).

    `session` holds the "allow always for this session" answers. A frontend keeps one
    object for the whole session and passes it to every turn; without one, the
    allowances last only for this call.
    `client_tools` is the editor's file and terminal methods, for a client that advertised them (T20).
    The tools use them instead of the local disk and shell; approval and the sandbox are unchanged.
    """
    settings = Settings() if settings is None else settings
    if client is None:
        client = ollama.Client(host=settings.host)
    approver = approver or DenyAll()
    cancel = cancel if cancel is not None else threading.Event()
    session = session if session is not None else SessionApprovals()
    usage = _Usage()
    # The user message this turn answers is the last one at entry. If the turn records no reply
    # at all (an Ollama error, or a cancel before the first reply), it is removed, so sending the
    # same text again does not put it into the history twice (TASKS.md, T04 and T11).
    start = len(messages)
    user_index = start - 1
    try:
        return _run_rounds(messages, client, settings, emit, approver, cancel, usage, session, client_tools)
    except KeyboardInterrupt:
        # Ctrl+C outside the guarded steps (for example while an event is being printed).
        # Keep the invariant even so, then end the turn as cancelled.
        cancel.set()
        _answer_open_calls(messages)
        try:
            emit(TurnDone(stop_reason="cancelled", text=None, usage=usage.value()))
        except KeyboardInterrupt:  # a second Ctrl+C while the cancel is reported: the history is already valid
            pass
        return None
    finally:
        if start > 0 and len(messages) == start and messages[user_index]["role"] == "user":
            del messages[user_index]


def _run_rounds(
    messages: list,
    client: ChatClient,
    settings: Settings,
    emit: EventSink,
    approver: Approver,
    cancel: threading.Event,
    usage: _Usage,
    session: SessionApprovals,
    client_tools: ClientTools | None = None,
) -> str | None:
    tool_context = ToolContext(command_timeout_s=settings.command_timeout_s,
                               max_output_chars=settings.max_output_chars,
                               workspace=settings.workspace,
                               test_command=settings.test_command,
                               network_timeout_s=settings.network_timeout_s,
                               cancel=cancel,
                               plugin_dir=plugin_dir_of(settings),
                               client=client_tools)
    # The tools offered this turn (by the profile and the network switch). Calls to others are refused.
    offered = profiles.offered_tools(tools.TOOLS, settings)
    for round_no in range(1, settings.max_tool_rounds + 1):
        if cancel.is_set():
            emit(TurnDone(stop_reason="cancelled", text=None, rounds=round_no - 1, usage=usage.value()))
            return None

        try:
            msg = _stream_reply(client, messages, settings, emit, cancel, usage, offered)
        except KeyboardInterrupt:
            cancel.set()
            msg = None
        except OLLAMA_ERRORS as e:
            emit(Error(message=describe_error(e, settings)))
            return None
        if msg is None:  # cancelled while streaming: the partial reply is dropped
            emit(TurnDone(stop_reason="cancelled", text=None, rounds=round_no, usage=usage.value()))
            return None
        messages.append(msg)

        # No tool calls: the reply is the final answer for this turn.
        calls = msg.get("tool_calls")
        if not calls:
            text = msg.get("content") or ""
            emit(TurnDone(stop_reason="end_turn", text=text, rounds=round_no, usage=usage.value()))
            return text

        # Execute each requested tool and feed results back to the model.
        if _run_calls(calls, round_no, messages, emit, approver, cancel, tool_context, settings.workspace,
                      session, offered, settings):
            emit(TurnDone(stop_reason="cancelled", text=None, rounds=round_no, usage=usage.value()))
            return None

    emit(TurnDone(stop_reason="max_turn_requests", text=None, rounds=settings.max_tool_rounds,
                  usage=usage.value()))
    return None


def _stream_reply(
    client: ChatClient,
    messages: list,
    settings: Settings,
    emit: EventSink,
    cancel: threading.Event,
    usage: _Usage,
    offered: dict[str, Tool],
):
    """Stream one model reply, emitting its text. Returns the complete assistant message.

    Returns None if cancelled before the stream finished. Connection and model
    errors propagate to run_turn. Thinking is kept in the message, so it stays
    in history, and shown only as an indicator.
    """
    parts: list[str] = []
    thinking: list[str] = []
    tool_calls: list = []
    stream = client.chat(
        model=settings.model,
        messages=messages,
        tools=_tool_schemas(offered),
        stream=True,
        options={"num_ctx": settings.num_ctx},
    )
    try:
        for chunk in stream:
            if cancel.is_set():
                return None
            part = chunk["message"]
            text = part.get("content") or ""
            if text:
                parts.append(text)
                emit(TextDelta(text=text))
            reasoning = part.get("thinking") or ""
            if reasoning:
                thinking.append(reasoning)
                emit(ThinkingDelta(text=reasoning))
            # Tool calls may arrive in any chunk, so collect them across the whole stream.
            tool_calls.extend(part.get("tool_calls") or [])
            if chunk.done:  # the last chunk carries the token counts
                usage.add(chunk.prompt_eval_count, chunk.eval_count)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()

    content = "".join(parts)
    if not tool_calls:
        tool_calls = parse_text_tool_calls(content, tools.TOOLS) or []
    return ollama.Message(
        role="assistant",
        content=content,
        tool_calls=tool_calls or None,
        thinking="".join(thinking) or None,
    )


def _run_calls(
    calls: list,
    round_no: int,
    messages: list,
    emit: EventSink,
    approver: Approver,
    cancel: threading.Event,
    tool_context: ToolContext,
    workspace: Path,
    session: SessionApprovals,
    offered: dict[str, Tool],
    settings: Settings,
) -> bool:
    """Run the tool calls of one reply. Returns True if the turn was cancelled.

    Every call gets a tool message, even after a cancel: calls that did not run
    get CANCELLED_MESSAGE as a failed result. A call to a registered tool that the
    profile or the network switch does not offer is refused here, without a question.
    """
    for index, call in enumerate(calls, start=1):
        name = call["function"]["name"]
        args = dict(call["function"]["arguments"] or {})
        call_id = f"{round_no}.{index}"
        tool = tools.TOOLS.get(name)
        emit(ToolCall(tool_call_id=call_id, name=name, arguments=args,
                      kind=tool.kind if tool else "other", title=_call_title(name, args)))
        blocked = profiles.blocked_message(name, tool, offered, settings)
        if cancel.is_set():
            content, status = CANCELLED_MESSAGE, "failed"
        elif blocked is not None:
            content, status = blocked, "failed"
        else:
            try:
                content, status = _run_tool(tool, name, call_id, args, emit, approver, tool_context,
                                            workspace, session)
            except KeyboardInterrupt:  # Ctrl+C at an approval prompt or while a tool runs
                cancel.set()
                content, status = CANCELLED_MESSAGE, "failed"
        emit(ToolResult(tool_call_id=call_id, name=name, content=content, status=status))
        messages.append({"role": "tool", "content": content, "tool_name": name})
    return cancel.is_set()


TITLE_CHARS = 120


def _call_title(name: str, arguments: dict) -> str:
    """A short label for a tool call, for frontends that need one (ACP tool_call.title)."""
    if not arguments:
        return name
    text = f"{name}({', '.join(f'{key}={value!r}' for key, value in arguments.items())})"
    return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 3] + "..."


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


def _tool_schemas(offered: dict[str, Tool]) -> list:
    return [t.schema() for t in offered.values()]


def _run_tool(
    tool: Tool | None,
    name: str,
    call_id: str,
    args: dict,
    emit: EventSink,
    approver: Approver,
    tool_context: ToolContext,
    workspace: Path,
    session: SessionApprovals,
) -> tuple[str, ToolStatus]:
    """Run one tool call after the sandbox and approval policy. Failures come back as text for the model.

    Order: bind the arguments; resolve the path arguments (refused paths stop here, unasked);
    ask for each path outside the workspace; run the tool's precheck (a call that cannot
    succeed is answered here, before the tool's own question); ask for the tool's own risk
    unless the session allows this tool in this workspace; then run with the resolved paths.

    The outside-workspace question is never skipped by a session allowance, and it comes
    before the precheck, because the precheck reads the file.
    """
    if tool is None:
        return f"Unknown tool: {name}", "failed"
    if tool.location is not Location.CORE:
        return f"Tool {name} runs on the client, and no client is connected.", "failed"
    try:
        # Bind first: bad arguments are reported before the user is asked, and with the real tool's name.
        tool.bind(args, tool_context)
        try:
            args, outside = sandbox.check_arguments(tool.path_args, args, workspace)
        except sandbox.PathRefused as e:
            return f"Refused: {e}.", "failed"
        for item in outside:
            request = sandbox.outside_request(call_id, tool.name, tool.risk.value, item, workspace)
            if not _ask(request, emit, approver).allowed:
                return sandbox.outside_denied_message(item, workspace), "failed"
        problem = tool.precheck(args, tool_context)
        if problem is not None:
            return problem, "failed"
        may_stay = tool.session_allowed_for(args, tool_context)
        if needs_approval(tool.risk) and not (may_stay and session.allows(tool.name, workspace)):
            request = tool.approval_request(call_id, args, tool_context)
            decision = _ask(request, emit, approver)
            if not decision.allowed:
                return tool.denied_message, "failed"
            if decision is Decision.ALLOW_ALWAYS and "allow_always" in request.options:
                session.allow(tool.name, workspace)
        emit(ToolStarted(tool_call_id=call_id, name=name))
        return tool.call(args, tool_context), "completed"
    except ToolCancelled:  # a tool that stopped for a cancel (for example an MCP call), same answer as the core's own
        return CANCELLED_MESSAGE, "failed"
    except Exception as e:
        return f"Error calling {name} with arguments {args}: {type(e).__name__}: {e}", "failed"


def _ask(request: ApprovalRequest, emit: EventSink, approver: Approver) -> Decision:
    """Show the request to the sink, then ask the approver. Anything but a known answer refuses.

    An approver that raises has approved nothing. An "allow always" answer to a request that does not
    offer it counts as allow once.
    """
    emit(request)
    try:
        answer = approver.decide(request)
    except Exception:
        return Decision.REJECT_ONCE
    if not isinstance(answer, str):  # None, a bool or a number is not an answer: refused
        return Decision.REJECT_ONCE
    try:
        decision = Decision(answer)
    except ValueError:  # a string that is not a known answer is refused
        return Decision.REJECT_ONCE
    if decision is Decision.ALLOW_ALWAYS and "allow_always" not in request.options:
        return Decision.ALLOW_ONCE
    return decision
