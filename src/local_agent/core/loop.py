"""The agent loop: one user turn of model <-> tool rounds.

The loop reports what happens as events (core/events.py) through an EventSink,
and asks an Approver before any write, exec or network tool runs. It never
prints or reads input; the frontend decides how to show events and answer.
"""

from typing import Any, Protocol

import ollama

from local_agent import tools
from local_agent.core.events import (
    Decision,
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


class ChatClient(Protocol):
    """The part of the Ollama client the loop uses. The `ollama` module itself fits."""

    def chat(self, *, model: str, messages: list, tools: list) -> Any: ...


def new_messages() -> list:
    """Fresh conversation history with the system prompt."""
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def run_turn(
    messages: list,
    client: ChatClient = ollama,
    *,
    emit: EventSink = discard,
    approver: Approver | None = None,
) -> str | None:
    """Run one user turn. `messages` must already end with the user message.

    Appends the model replies and tool results to `messages`, and emits events
    to `emit`. Write, exec and network tools are approved by `approver`; without
    one, they are refused. Returns the final reply text, or None if the turn hit
    MAX_TOOL_ROUNDS.
    """
    approver = approver or DenyAll()
    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        response = client.chat(
            model=MODEL,
            messages=messages,
            tools=[t.schema() for t in tools.TOOLS.values()],
        )
        msg = response["message"]
        messages.append(msg)

        # No tool calls: the reply is the final answer for this turn.
        calls = msg.get("tool_calls")
        if not calls:
            text = msg.get("content") or ""
            emit(TextDelta(text=text))
            emit(TurnDone(stop_reason="end_turn", text=text, rounds=round_no))
            return text

        # Execute each requested tool and feed results back to the model.
        for index, call in enumerate(calls, start=1):
            name = call["function"]["name"]
            args = dict(call["function"]["arguments"] or {})
            call_id = f"{round_no}.{index}"
            tool = tools.TOOLS.get(name)
            emit(ToolCall(tool_call_id=call_id, name=name, arguments=args,
                          kind=tool.kind if tool else "other"))
            content, status = _run_tool(tool, name, call_id, args, emit, approver)
            emit(ToolResult(tool_call_id=call_id, name=name, content=content, status=status))
            messages.append({"role": "tool", "content": content})

    emit(TurnDone(stop_reason="max_turn_requests", text=None, rounds=MAX_TOOL_ROUNDS))
    return None


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
