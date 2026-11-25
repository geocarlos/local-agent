"""The stdio server: `local-agent serve --stdio`, the Agent Client Protocol (ACP) over JSON-RPC 2.0.

One JSON object per line in each direction, UTF-8, `\\n`-terminated. The wire shapes follow ACP
protocol version 1 (checked against the v1 schema; see docs/protocol.md and TASKS.md, T11).

Threads: the main thread reads stdin and answers requests. A `session/prompt` runs its turn on a
worker thread, so `session/cancel` and permission answers arrive while the turn runs. The core
(`core/loop.py`) never prints; this module is the only place that writes protocol messages, and
stdout carries nothing else. Warnings and log lines go to stderr.

Mapping of core events to ACP:
    TextDelta        -> session/update agent_message_chunk (messageId per run of text)
    ThinkingDelta    -> session/update agent_thought_chunk
    ToolCall         -> session/update tool_call          (status pending)
    ApprovalRequest  -> request session/request_permission (answered by the client)
    ToolStarted      -> session/update tool_call_update   (status in_progress)
    ToolResult       -> session/update tool_call_update   (status completed or failed)
    TurnDone         -> session/update usage_update (context size), then the session/prompt result
    Error            -> the session/prompt error response
"""

import contextlib
import dataclasses
import io
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, TextIO

import ollama

from local_agent.config import Settings
from local_agent.core import loop
from local_agent.core.events import (
    ApprovalRequest,
    Decision,
    Error,
    Event,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    ToolStarted,
    TurnDone,
)
from local_agent.core.model import OLLAMA_ERRORS, describe_error, probe
from local_agent.permissions import SessionApprovals
from local_agent.tools import mcp_tools, plugins

PROTOCOL_VERSION = 1
JSONRPC = "2.0"

# JSON-RPC and ACP error codes (ACP error page, protocol v1).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002

# The labels a client shows for each permission option. The option id is the ACP kind.
PERMISSION_LABELS = {
    "allow_once": "Allow once",
    "allow_always": "Allow always for this session",
    "reject_once": "Reject",
}

# How long the main loop waits for a permission answer before it checks for a cancel.
_POLL_S = 0.05
# One deadline, shared by all running turns, for shutdown to wait for them after their cancel is set.
SHUTDOWN_DEADLINE_S = 10.0
PROBE_JOIN_S = 5.0  # at shutdown, how long to wait for the startup model check to report
# At most this many sessions per server process. A session/new beyond it gets -32600.
MAX_SESSIONS = 64
# A line longer than this (in characters, 16 MiB of text) is refused with -32600 and discarded.
MAX_LINE_CHARS = 16 * 1024 * 1024
# Stands for a line over MAX_LINE_CHARS that the bounded reader discarded.
OVERSIZE_LINE = object()


class ProtocolError(Exception):
    """A request that cannot be answered as asked. Becomes a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _result(request_id: Any, value: Any) -> dict:
    return {"jsonrpc": JSONRPC, "id": request_id, "result": value}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    body: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": JSONRPC, "id": request_id, "error": body}


def _notification(method: str, params: dict) -> dict:
    return {"jsonrpc": JSONRPC, "method": method, "params": params}


def _request(request_id: str, method: str, params: dict) -> dict:
    return {"jsonrpc": JSONRPC, "id": request_id, "method": method, "params": params}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _agent_version() -> str:
    try:
        return version("local-agent")
    except PackageNotFoundError:
        return "0.0.0"


@dataclass
class _Session:
    session_id: str
    settings: Settings                     # the server's settings with this session's workspace
    messages: list                         # the conversation history
    approvals: SessionApprovals = field(default_factory=SessionApprovals)
    cancel: threading.Event = field(default_factory=threading.Event)
    busy: bool = False                     # a prompt turn is running
    turn: int = 0                          # counts prompts, so tool call ids stay unique in the session


@dataclass
class _Pending:
    """A session/request_permission waiting for the client's answer."""

    event: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None              # the selected optionId, or None for cancelled or unusable


class _TurnEvents:
    """The event sink for one prompt turn: writes session/update notifications and records how the turn ended."""

    def __init__(self, server: "StdioServer", session: _Session, turn: int) -> None:
        self._server = server
        self._session = session
        self._turn = turn
        self._chunk: tuple[str, str] | None = None  # (sessionUpdate kind, messageId) of the open chunk run
        self.outcome: TurnDone | Error | None = None

    def _tool_id(self, call_id: str) -> str:
        return f"t{self._turn}-{call_id}"

    def __call__(self, event: Event) -> None:
        sid = self._session.session_id
        if isinstance(event, (TextDelta, ThinkingDelta)):
            kind = "agent_message_chunk" if isinstance(event, TextDelta) else "agent_thought_chunk"
            if self._chunk is None or self._chunk[0] != kind:
                self._chunk = (kind, uuid.uuid4().hex)  # a new message starts after a tool step or a switch of kind
            self._server.notify_update(sid, {"sessionUpdate": kind, "content": _text(event.text),
                                             "messageId": self._chunk[1]})
            return
        self._chunk = None
        if isinstance(event, ToolCall):
            update: dict | None = {"sessionUpdate": "tool_call", "toolCallId": self._tool_id(event.tool_call_id),
                                   "title": event.title or event.name, "kind": event.kind, "status": "pending",
                                   "rawInput": event.arguments}
        elif isinstance(event, ToolStarted):
            update = {"sessionUpdate": "tool_call_update", "toolCallId": self._tool_id(event.tool_call_id),
                      "status": "in_progress"}
        elif isinstance(event, ToolResult):
            update = {"sessionUpdate": "tool_call_update", "toolCallId": self._tool_id(event.tool_call_id),
                      "status": event.status, "content": [{"type": "content", "content": _text(event.content)}]}
        elif isinstance(event, TurnDone):
            self.outcome = event
            update = None
            if event.usage is not None:
                # ACP has no token counts; usage_update carries the context size (used of size).
                self._server.notify_update(sid, {"sessionUpdate": "usage_update", "used": event.usage.prompt_tokens,
                                                 "size": self._session.settings.num_ctx})
        elif isinstance(event, Error):
            self.outcome = event
            update = None
        else:  # ApprovalRequest: the approver sends session/request_permission itself
            update = None
        if update is not None:
            self._server.notify_update(sid, update)


class _ProtocolApprover:
    """Asks the client with session/request_permission and waits for its answer (or for a cancel)."""

    def __init__(self, server: "StdioServer", session: _Session, turn: int) -> None:
        self._server = server
        self._session = session
        self._turn = turn

    def decide(self, request: ApprovalRequest) -> Decision:
        request_id, pending = self._server.start_permission_request(self._session, self._turn, request)
        while not pending.event.wait(_POLL_S):
            if self._session.cancel.is_set():  # the turn was cancelled: the core will end it as cancelled
                break
        self._server.finish_permission_request(request_id)
        if pending.answer is None or pending.answer not in request.options:
            return Decision.REJECT_ONCE
        return Decision(pending.answer)


class StdioServer:
    """The ACP server over a line reader and a text writer. `serve()` runs until the reader ends.

    `reader` is an iterable of text lines; `writer` takes protocol lines only; `log` takes warning
    lines for stderr. The caller starts and closes the plugins and MCP servers (see serve_stdio).
    """

    def __init__(self, settings: Settings, *, client: Any, reader: Iterable[str], writer: TextIO,
                 log: Callable[[str], None]) -> None:
        self._settings = settings
        self._client = client
        self._reader = reader
        self._writer = writer
        self._log = log
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()  # guards sessions, pending requests, the initialized flag and busy flags
        self._sessions: dict[str, _Session] = {}
        self._pending: dict[str, _Pending] = {}
        self._initialized = False
        self._request_count = 0
        self._workers: list[threading.Thread] = []
        self._writes_failed = False

    # ----- reading ---------------------------------------------------------------------------------

    def serve(self) -> None:
        """Handle lines until the reader ends (EOF). A running turn is cancelled, then joined."""
        try:
            for line in self._reader:
                try:
                    if line is OVERSIZE_LINE:
                        self._send(_error(None, INVALID_REQUEST,
                                          f"Invalid request: a line is longer than {MAX_LINE_CHARS} characters."))
                    else:
                        self._handle_line(line)
                except Exception as e:  # a bug must not end the server: report it and go on
                    self._log(f"Error: internal error while handling a message: {type(e).__name__}: {e}")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        """Cancel every session, then wait for the running turns: one deadline for all of them together."""
        with self._lock:
            for session in self._sessions.values():
                session.cancel.set()
            workers = list(self._workers)
        deadline = time.monotonic() + SHUTDOWN_DEADLINE_S
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        still_running = [worker.name for worker in workers if worker.is_alive()]
        if still_running:
            self._log(f"Warning: {len(still_running)} turn(s) did not stop within {SHUTDOWN_DEADLINE_S:g} s "
                      f"after the cancel; closing anyway ({', '.join(still_running)}).")

    def _handle_line(self, line: str) -> None:
        if len(line) > MAX_LINE_CHARS:  # a reader that does not bound lines (tests, injected streams)
            self._send(_error(None, INVALID_REQUEST,
                              f"Invalid request: a line is longer than {MAX_LINE_CHARS} characters."))
            return
        text = line.strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except (ValueError, RecursionError):  # deeply nested JSON exhausts the parser's recursion too
            self._send(_error(None, PARSE_ERROR, "Parse error: the line is not valid JSON."))
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != JSONRPC:
            request_id = message.get("id") if isinstance(message, dict) else None
            if isinstance(message, dict) and "id" not in message:
                self._log("Warning: ignored a message that is not JSON-RPC 2.0.")
                return
            self._send(_error(request_id, INVALID_REQUEST, "Invalid request: expected a JSON-RPC 2.0 object."))
            return
        if "method" not in message:
            if "id" in message and ("result" in message or "error" in message):
                self._on_response(message)
            else:
                self._send(_error(message.get("id"), INVALID_REQUEST, "Invalid request: no method."))
            return
        method = message["method"]
        if not isinstance(method, str):
            self._send(_error(message.get("id"), INVALID_REQUEST, "Invalid request: method must be a string."))
            return
        params = message.get("params", {})
        if "id" not in message:
            self._on_notification(method, params)
            return
        request_id = message["id"]
        try:
            self._on_request(request_id, method, params)
        except ProtocolError as e:
            self._send(_error(request_id, e.code, e.message, e.data))

    def _on_request(self, request_id: Any, method: str, params: Any) -> None:
        handlers = {
            "initialize": self._initialize,
            "session/new": self._new_session,
            "session/prompt": self._prompt,
        }
        handler = handlers.get(method)
        if handler is None:
            raise ProtocolError(METHOD_NOT_FOUND, f"Method not found: {method}")
        if not isinstance(params, dict):
            raise ProtocolError(INVALID_PARAMS, "Invalid params: expected an object.")
        handler(request_id, params)

    def _on_notification(self, method: str, params: Any) -> None:
        if method == "session/cancel":
            session = self._session_of(params) if isinstance(params, dict) else None
            if session is not None and session.busy:
                session.cancel.set()
            return
        self._log(f"Warning: ignored notification {method!r}.")

    def _on_response(self, message: dict) -> None:
        """An answer to one of our requests (only session/request_permission)."""
        response_id = message["id"]
        if not isinstance(response_id, str):  # our request ids are strings; a list or dict would not hash
            self._log(f"Warning: ignored a response with an id that is not a string: {json.dumps(response_id)[:80]}")
            return
        with self._lock:
            pending = self._pending.get(response_id)
        if pending is None:
            self._log(f"Warning: ignored a response with an unknown id {message['id']!r}.")
            return
        answer = None
        result = message.get("result")
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if isinstance(outcome, dict) and outcome.get("outcome") == "selected" and isinstance(outcome.get("optionId"), str):
            answer = outcome["optionId"]
        pending.answer = answer
        pending.event.set()

    # ----- requests --------------------------------------------------------------------------------

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ProtocolError(INVALID_REQUEST, "Send initialize before other requests.")

    def _initialize(self, request_id: Any, params: dict) -> None:
        requested = params.get("protocolVersion")
        if not isinstance(requested, int) or isinstance(requested, bool):
            raise ProtocolError(INVALID_PARAMS, "Invalid params: protocolVersion must be an integer.")
        # The agent answers with the one version it speaks; the client decides whether to continue.
        with self._lock:
            self._initialized = True
        self._send(_result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
            },
            "authMethods": [],
            "agentInfo": {"name": "local-agent", "version": _agent_version()},
        }))

    def _new_session(self, request_id: Any, params: dict) -> None:
        self._require_initialized()
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            raise ProtocolError(INVALID_PARAMS, "Invalid params: cwd must be an absolute path.")
        folder = Path(cwd)
        if not folder.is_dir():
            raise ProtocolError(INVALID_PARAMS, f"Invalid params: cwd is not an existing folder: {cwd}")
        servers = params.get("mcpServers")
        if not isinstance(servers, list):
            raise ProtocolError(INVALID_PARAMS, "Invalid params: mcpServers must be a list.")
        if servers:  # not supported yet: the servers the client names are not started
            self._log(f"Warning: session/new named {len(servers)} MCP server(s); they are ignored. "
                      "Configure servers in config.toml instead.")
        session = _Session(session_id=f"sess-{uuid.uuid4().hex}",
                           settings=dataclasses.replace(self._settings, workspace=folder),
                           messages=loop.new_messages())
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise ProtocolError(INVALID_REQUEST, f"Too many sessions: this server keeps at most {MAX_SESSIONS} "
                                                     "per process. Start another server for more.")
            self._sessions[session.session_id] = session
        self._send(_result(request_id, {"sessionId": session.session_id}))

    def _prompt(self, request_id: Any, params: dict) -> None:
        self._require_initialized()
        session = self._session_of(params, required=True)
        text = _prompt_text(params.get("prompt"))
        with self._lock:
            if session.busy:
                raise ProtocolError(INVALID_REQUEST, "A prompt is already running in this session.")
            session.busy = True
            session.turn += 1
            turn = session.turn
            session.cancel.clear()
            worker = threading.Thread(target=self._run_turn, args=(session, request_id, turn, text),
                                      name=f"turn-{session.session_id}-{turn}", daemon=True)
            self._workers = [w for w in self._workers if w.is_alive()] + [worker]  # finished turns are pruned
        worker.start()

    def _session_of(self, params: dict, *, required: bool = False) -> _Session | None:
        sid = params.get("sessionId")
        with self._lock:
            session = self._sessions.get(sid) if isinstance(sid, str) else None
        if session is None and required:
            raise ProtocolError(RESOURCE_NOT_FOUND, "Session not found.", {"sessionId": sid})
        return session

    # ----- the turn --------------------------------------------------------------------------------

    def _run_turn(self, session: _Session, request_id: Any, turn: int, text: str) -> None:
        events = _TurnEvents(self, session, turn)
        approver = _ProtocolApprover(self, session, turn)
        response: dict
        try:
            session.messages.append({"role": "user", "content": text})
            loop.run_turn(session.messages, client=self._client, settings=session.settings, emit=events,
                          approver=approver, cancel=session.cancel, session=session.approvals)
            outcome = events.outcome
            if isinstance(outcome, TurnDone):
                response = _result(request_id, _prompt_result(outcome))
            elif isinstance(outcome, Error):
                response = _error(request_id, INTERNAL_ERROR, outcome.message, {"kind": "agent"})
            else:
                response = _error(request_id, INTERNAL_ERROR, "The turn ended without a result.")
        except Exception as e:
            self._log(f"Error: the turn failed: {type(e).__name__}: {e}")
            response = _error(request_id, INTERNAL_ERROR, f"Internal error: {type(e).__name__}: {e}")
        finally:
            with self._lock:
                session.busy = False  # before the response, so the client may send the next prompt at once
        self._send(response)

    def start_permission_request(self, session: _Session, turn: int,
                                 request: ApprovalRequest) -> tuple[str, _Pending]:
        """Send session/request_permission for one approval; returns its id and the waiter for the answer."""
        with self._lock:
            self._request_count += 1
            request_id = f"srv-{self._request_count}"
            pending = _Pending()
            self._pending[request_id] = pending
        options = [{"optionId": option, "name": PERMISSION_LABELS.get(option, option), "kind": option}
                   for option in request.options]
        self._send(_request(request_id, "session/request_permission", {
            "sessionId": session.session_id,
            "toolCall": {"toolCallId": f"t{turn}-{request.tool_call_id}", "title": request.title,
                         "status": "pending", "rawInput": request.arguments},
            "options": options,
        }))
        return request_id, pending

    def finish_permission_request(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    # ----- writing ---------------------------------------------------------------------------------

    def notify_update(self, session_id: str, update: dict) -> None:
        self._send(_notification("session/update", {"sessionId": session_id, "update": update}))

    def _send(self, message: dict) -> None:
        line = json.dumps(message, default=str) + "\n"  # ASCII-only output (ensure_ascii), so any stdout encoding works
        with self._write_lock:
            if self._writes_failed:
                return
            try:
                self._writer.write(line)
                self._writer.flush()
            except (OSError, ValueError) as e:  # the client has gone away: nothing more can be sent
                self._writes_failed = True
                self._log(f"Warning: cannot write to stdout ({type(e).__name__}: {e}); no more messages are sent.")


def _prompt_text(blocks: Any) -> str:
    """The text of a session/prompt. This agent takes text blocks only (it declares no other prompt capability)."""
    if not isinstance(blocks, list) or not blocks:
        raise ProtocolError(INVALID_PARAMS, "Invalid params: prompt must be a non-empty list of content blocks.")
    parts = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            kind = block.get("type") if isinstance(block, dict) else type(block).__name__
            raise ProtocolError(INVALID_PARAMS, f"Invalid params: unsupported content block {kind!r}; "
                                                "this agent accepts text only.")
        parts.append(block["text"])
    text = "\n".join(parts).strip()
    if not text:
        raise ProtocolError(INVALID_PARAMS, "Invalid params: the prompt is empty.")
    try:
        text.encode("utf-8")  # a lone surrogate (JSON "\ud800") is valid JSON but cannot be sent to Ollama
    except UnicodeEncodeError:
        raise ProtocolError(INVALID_PARAMS, "Invalid params: the prompt text is not valid Unicode.") from None
    return text


def _prompt_result(done: TurnDone) -> dict:
    result: dict[str, Any] = {"stopReason": done.stop_reason}
    if done.usage is not None:  # ACP has no token counts in the prompt result, so they go under _meta
        result["_meta"] = {"usage": {"promptTokens": done.usage.prompt_tokens,
                                     "completionTokens": done.usage.completion_tokens}}
    return result


def _bounded_lines(stream: TextIO, limit: int) -> Iterator[Any]:
    """The lines of `stream`, each at most `limit` characters. A longer line is read to its end and
    discarded; it is yielded as OVERSIZE_LINE, so memory use stays bounded."""
    while True:
        line = stream.readline(limit + 1)
        if not line:
            return
        if len(line) <= limit or line.endswith("\n"):
            yield line
            continue
        while True:  # discard the rest of the long line
            rest = stream.readline(65536)
            if not rest or rest.endswith("\n"):
                break
        yield OVERSIZE_LINE


def serve_stdio(settings: Settings, *, client: Any = None, reader: Iterable[str] | None = None,
                writer: TextIO | None = None, errors: TextIO | None = None) -> None:
    """Run the stdio server until stdin ends. Plugins and MCP servers start first and close last.

    With no `writer`, the real stdout carries the protocol and the process's own stdout is pointed at
    stderr (file descriptor 1 is redirected with dup2, so os.system, subprocess and os.write(1) cannot
    write into the protocol). The original stdout is restored on return. `sys.stdout` is redirected to
    stderr too, for print. With no `reader`, stdin is read with a line-length cap. Tests pass their own
    streams and `errors`, and then nothing about the process's file descriptors changes.
    """
    errors = sys.stderr if errors is None else errors

    def log(line: str) -> None:
        print(line, file=errors, flush=True)

    owned_fd = None      # the protocol's copy of the real stdout (fd 1 before the redirect)
    owned_writer = None
    if writer is None:
        owned_fd = os.dup(1)
        os.dup2(2, 1)    # fd 1 now points at stderr, for this process and its children
        owned_writer = io.TextIOWrapper(os.fdopen(owned_fd, "wb"), encoding="utf-8", newline="\n", write_through=True)
        writer = owned_writer
    if reader is None:
        reader = _bounded_lines(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"), MAX_LINE_CHARS)
    if client is None:
        client = ollama.Client(host=settings.host)

    try:
        with contextlib.redirect_stdout(errors):
            plugin_runtime = None
            mcp = None
            probe_thread = None
            try:
                plugin_runtime = plugins.load_plugins(settings)
                mcp = mcp_tools.start_mcp_servers(settings.mcp_servers, timeout_s=settings.command_timeout_s)
                for line in plugin_runtime.warnings + mcp.warnings:
                    log(line)
                summary = plugins.describe_loaded(plugin_runtime)
                if summary is not None:
                    log(summary)
                # The model check runs beside the server, so a slow or unreachable Ollama cannot delay initialize.
                probe_thread = threading.Thread(target=_probe_in_background, args=(client, settings, log),
                                                name="startup-probe", daemon=True)
                probe_thread.start()
                server = StdioServer(settings, client=client, reader=reader, writer=writer, log=log)
                server.serve()
            except KeyboardInterrupt:  # Ctrl+C ends the server like EOF does
                pass
            finally:
                if probe_thread is not None:  # let a quick model check finish, so its warning is not lost
                    probe_thread.join(PROBE_JOIN_S)
                if mcp is not None:
                    mcp.close()
                if plugin_runtime is not None:
                    plugin_runtime.close()
    finally:
        if owned_writer is not None:
            with contextlib.suppress(ValueError, OSError):
                owned_writer.flush()
            with contextlib.suppress(OSError):
                os.dup2(owned_fd, 1)  # give the process its real stdout back
            with contextlib.suppress(ValueError, OSError):
                owned_writer.close()


def _probe_in_background(client: Any, settings: Settings, log: Callable[[str], None]) -> None:
    """Thread target for the model check. A failure here is a warning, never a crash."""
    try:
        _log_startup_probe(client, settings, log)
    except Exception as e:
        with contextlib.suppress(Exception):
            log(f"Warning: the model check failed: {type(e).__name__}: {e}")


def _log_startup_probe(client: Any, settings: Settings, log: Callable[[str], None]) -> None:
    """The model check of the chat REPL, reported on stderr. The server runs on either way."""
    try:
        warnings = probe(client, settings)
    except OLLAMA_ERRORS as e:
        log(f"Warning: {describe_error(e, settings)}")
        return
    for line in warnings:
        log(line)
