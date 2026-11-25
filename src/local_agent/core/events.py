"""Events the core sends to a frontend, and the approval request/decision pair.

The core never prints or reads input. It reports what happens through an
`EventSink` (a callable that takes one event) and asks an `Approver` before
risky tool calls (see local_agent.permissions).

Names follow the event table in PLAN.md §1. Fields are aligned with the Agent
Client Protocol (ACP) so a later stdio server (T11) can map them 1:1:

    TextDelta       -> session/update  agent_message_chunk
    ToolCall        -> session/update  tool_call         (tool_call_id, kind, raw input)
    ToolResult      -> session/update  tool_call_update  (status completed/failed)
    ApprovalRequest -> session/request_permission        (options allow_once/reject_once)
    TurnDone        -> session/prompt response           (stop_reason)
    Error           -> JSON-RPC error

See TASKS.md, notes for T02.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Literal

# ACP ToolKind values (full set). A tool's kind is derived from its risk tier (see tools/registry.py).
ToolKind = Literal[
    "read", "edit", "delete", "move", "search", "execute", "think", "fetch", "switch_mode", "other"
]

# ACP StopReason values (full set). The core emits end_turn, max_turn_requests and cancelled.
StopReason = Literal["end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"]

# ACP ToolCallStatus values for a finished call.
ToolStatus = Literal["completed", "failed"]

# Answers offered for a tool's own approval request (the outside-workspace request offers only allow/reject once).
TOOL_APPROVAL_OPTIONS = ("allow_once", "allow_always", "reject_once")


@dataclass(frozen=True)
class TextDelta:
    """A piece of model text, sent as it streams. Joined in order, the deltas of one reply are its text."""

    text: str
    type: ClassVar[str] = "text_delta"


@dataclass(frozen=True)
class ThinkingDelta:
    """A piece of the model's reasoning, sent as it streams. Shown as an indicator only; it is kept in history."""

    text: str
    type: ClassVar[str] = "thinking_delta"


@dataclass(frozen=True)
class ToolCall:
    """The model asked for a tool. Sent before approval and execution."""

    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    kind: ToolKind = "other"
    type: ClassVar[str] = "tool_call"


@dataclass(frozen=True)
class ToolResult:
    """A tool call finished. `content` is exactly what goes back to the model."""

    tool_call_id: str
    name: str
    content: str
    status: ToolStatus = "completed"
    type: ClassVar[str] = "tool_result"


@dataclass(frozen=True)
class Preview:
    """What the user should see before approving.

    `kind="text"` is proposed file content (with `path`). `kind="command"` is the
    command line to run, with `shell` naming the shell ("PowerShell" or "cmd").
    `kind="diff"` is a unified diff (with `path` for a file; no path for a commit
    summary). `text` is already truncated for display.
    """

    text: str
    path: str | None = None
    kind: Literal["text", "command", "diff"] = "text"
    shell: str | None = None


@dataclass(frozen=True)
class ApprovalRequest:
    """Ask the user before a write, exec or network tool runs.

    `title` is the question without the `(y/n)` suffix, for example
    "Write 12 chars to 'a.txt'". The core emits this event to the sink (so a
    frontend can show the preview) and then passes it to the Approver for a decision.

    `scope` is "tool" for the tool's own risk (write, exec, network) and
    "outside_workspace" for a path argument that resolves outside the workspace
    (see local_agent.sandbox). The second request is asked separately, so a
    write outside the workspace asks twice.

    `options` lists the answers the frontend may offer, as ACP PermissionOption
    kinds. A tool's own request offers "allow_always" (allow for this session, for
    this tool in this workspace). An outside-workspace request never does.
    """

    tool_call_id: str
    tool_name: str
    risk: str
    title: str
    arguments: dict[str, Any] = field(default_factory=dict)
    preview: Preview | None = None
    scope: Literal["tool", "outside_workspace"] = "tool"
    options: tuple[str, ...] = ("allow_once", "reject_once")
    type: ClassVar[str] = "approval_request"


class Decision(StrEnum):
    """The answer to an ApprovalRequest. Values are ACP PermissionOption kinds.

    ALLOW_ALWAYS is "allow for this session" for the tool and workspace of the
    request (see permissions.SessionApprovals). REJECT_ALWAYS is not offered yet.
    """

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    REJECT_ONCE = "reject_once"

    @property
    def allowed(self) -> bool:
        return self in (Decision.ALLOW_ONCE, Decision.ALLOW_ALWAYS)


@dataclass(frozen=True)
class Usage:
    """Token counts reported by Ollama for a turn, or None when Ollama gave none.

    `prompt_tokens` is the prompt size of the last model request, which is how
    much of the context window the conversation fills. `completion_tokens` is
    the number of tokens generated over all rounds of the turn.
    """

    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class TurnDone:
    """End of a user turn. `text` is the final reply, or None if there was none.

    `stop_reason="cancelled"` means the turn was cancelled (Ctrl+C or a cancel
    event). Tool calls that had not run are answered with a failed ToolResult.
    `usage` is set when the turn made at least one model request that reported counts.
    """

    stop_reason: StopReason
    text: str | None = None
    rounds: int = 0
    usage: Usage | None = None
    type: ClassVar[str] = "turn_done"


@dataclass(frozen=True)
class Error:
    """A failure the frontend should show, such as Ollama being unreachable. Tool failures are ToolResults, not Errors.

    An Error ends the turn: no TurnDone follows it (this matches ACP, where a failed prompt is a JSON-RPC error).
    """

    message: str
    type: ClassVar[str] = "error"


Event = TextDelta | ThinkingDelta | ToolCall | ToolResult | ApprovalRequest | TurnDone | Error

EventSink = Callable[[Event], None]


def discard(event: Event) -> None:
    """An EventSink that drops everything."""
