"""The ACP server: `wildcat-code-agent serve --stdio` (JSON lines on stdin and stdout) and the WebSocket
transport of `serve --web` (frontends/web.py). Both run the same `StdioServer` over a line reader and a
line writer, so the session, turn and approval logic exists once. The JSON-RPC 2.0 framing is ACP.

Stdio framing: one JSON object per line in each direction, UTF-8, `\\n`-terminated. The wire shapes follow ACP
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

from wildcat_code_agent import attachments
from wildcat_code_agent import config as config_module
from wildcat_code_agent import config_write, sessions
from wildcat_code_agent.config import ConfigError, Settings
from wildcat_code_agent.core import loop
from wildcat_code_agent.core.events import (
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
from wildcat_code_agent.client_tools import ClientError, CommandResult
from wildcat_code_agent.core.model import OLLAMA_ERRORS, describe_error, probe
from wildcat_code_agent.permissions import SessionApprovals
from wildcat_code_agent.tools import TOOLS, mcp_tools, plugins
from wildcat_code_agent.tools.registry import ToolCancelled

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
# How long the client has to answer a fs/* or terminal/* request (T20). A command's own wait uses the command timeout.
CLIENT_FILE_TIMEOUT_S = 30.0
CLIENT_CLEANUP_TIMEOUT_S = 5.0  # for terminal/output, terminal/kill and terminal/release
# One deadline, shared by all running turns, for shutdown to wait for them after their cancel is set.
SHUTDOWN_DEADLINE_S = 10.0
PROBE_JOIN_S = 5.0  # at shutdown, how long to wait for the startup model check to report
# At most this many sessions per server process. A session/new beyond it gets -32600.
MAX_SESSIONS = 64
# A line longer than this (in characters, 16 MiB of text) is refused with -32600 and discarded.
MAX_LINE_CHARS = 16 * 1024 * 1024
# Stands for a line over MAX_LINE_CHARS that the bounded reader discarded.
OVERSIZE_LINE = object()
# The model list and the model check call Ollama with a limit, so a silent Ollama cannot hang session/new.
OLLAMA_CALL_TIMEOUT_S = 5.0
# session/list returns this many sessions per page.
LIST_PAGE_SIZE = 50
# The one session config option: the model. Its values are the models Ollama has.
MODEL_OPTION_ID = "model"
# Extension methods (ACP reserves names starting with "_"; see docs/protocol.md).
EXT_MODELS = "_wildcat-code-agent/models"
EXT_SETTINGS_GET = "_wildcat-code-agent/settings/get"
EXT_SETTINGS_SET = "_wildcat-code-agent/settings/set"
# Agent to client (T21): the editor's diagnostics of the files attached to a prompt. Only when the client declares it.
DIAGNOSTICS_METHOD = "_wildcat-code-agent/diagnostics"


class TransportClosed(ValueError):
    """The writer cannot send any more: the client connection has closed. The server logs it and stops sending."""


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
        return version("wildcat-code-agent")
    except PackageNotFoundError:
        return "0.0.0"


def _tools_warning(model: str) -> str:
    return (f"Model '{model}' does not report the 'tools' capability. Tool calls may not work. "
            "Use a model with tool support, for example qwen3-vl:8b.")


def _optional_abs_path(value: Any, name: str) -> str | None:
    """An absolute path from a request field that may be absent (None), or -32602."""
    if value is None:
        return None
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ProtocolError(INVALID_PARAMS, f"Invalid params: {name} must be an absolute path.")
    return value


def _same_folder(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


@dataclass
class _Session:
    session_id: str
    settings: Settings                     # the server's settings with this session's workspace
    messages: list                         # the conversation history
    approvals: SessionApprovals = field(default_factory=SessionApprovals)
    cancel: threading.Event = field(default_factory=threading.Event)
    busy: bool = False                     # a prompt turn is running
    turn: int = 0                          # counts prompts, so tool call ids stay unique in the session
    created: str = field(default_factory=lambda: sessions.now_iso())  # ISO 8601, kept when the session is saved


@dataclass
class _Pending:
    """A request to the client waiting for its answer: session/request_permission, or a fs/* or terminal/* call."""

    event: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None              # the selected optionId, or None for cancelled or unusable
    result: Any = None                     # the result of a fs/* or terminal/* call
    has_result: bool = False               # whether the answer had a "result" member (null counts)
    error: dict | None = None              # the error of a fs/* or terminal/* call


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


class _SessionClient:
    """The client's file and terminal methods for one session's turns (T20). It implements client_tools.ClientTools.

    A flag is True only when the client advertised that capability in initialize. The tools check the path
    against the sandbox before they call in here, so nothing reaches the client that the core would refuse.
    """

    def __init__(self, server: "StdioServer", session: _Session, caps: dict) -> None:
        fs = caps.get("fs") if isinstance(caps.get("fs"), dict) else {}
        self._server = server
        self._session = session
        self.can_read = fs.get("readTextFile") is True
        self.can_write = fs.get("writeTextFile") is True
        self.can_terminal = caps.get("terminal") is True
        # The extension's diagnostics answer (T21): declared under clientCapabilities._meta, an ACP extension point.
        meta = caps.get("_meta") if isinstance(caps.get("_meta"), dict) else {}
        ext = meta.get("wildcat-code-agent") if isinstance(meta.get("wildcat-code-agent"), dict) else {}
        self.can_diagnostics = ext.get("diagnostics") is True

    def _call(self, method: str, params: dict, *, timeout_s: float | None = None,
              cancellable: bool = False) -> Any:
        limit = CLIENT_FILE_TIMEOUT_S if timeout_s is None else timeout_s  # read at call time, so a test can shorten it
        return self._server.call_client(self._session, method, params, timeout_s=limit, cancellable=cancellable)

    def read_text_file(self, path: str) -> str:
        result = self._call("fs/read_text_file", {"path": path}, cancellable=True)
        content = result.get("content") if isinstance(result, dict) else None
        if not isinstance(content, str):
            raise ClientError("the editor sent no text for the file")
        return content

    def diagnostics(self, uris: list[str]) -> dict[str, list[dict]]:
        """The diagnostics of the given files, from the editor (T21). Shape: attachments.clean_diagnostics."""
        result = self._call(DIAGNOSTICS_METHOD, {"uris": list(uris)}, cancellable=True)
        return attachments.clean_diagnostics(result)

    def write_text_file(self, path: str, content: str) -> bool:
        result = self._call("fs/write_text_file", {"path": path, "content": content}, cancellable=True)
        meta = result.get("_meta") if isinstance(result, dict) else None
        return not (isinstance(meta, dict) and meta.get("saved") is False)  # the editor says when it did not save

    def run_command(self, command: str, args: list[str], cwd: str, timeout_s: float) -> CommandResult:
        created = self._call("terminal/create", {"command": command, "args": list(args), "cwd": cwd})
        terminal_id = created.get("terminalId") if isinstance(created, dict) else None
        if not isinstance(terminal_id, str):
            raise ClientError("the editor did not return a terminal id")
        try:
            return self._collect(terminal_id, timeout_s)
        finally:
            self._cleanup("terminal/release", terminal_id)

    def _collect(self, terminal_id: str, timeout_s: float) -> CommandResult:
        exit_code: int | None = None
        timed_out = False
        try:
            waited = self._call("terminal/wait_for_exit", {"terminalId": terminal_id}, timeout_s=timeout_s,
                                cancellable=True)
            exit_code = _exit_code(waited)
        except ClientError as e:
            if not e.timed_out:
                raise
            timed_out = True
        except ToolCancelled:
            self._cleanup("terminal/kill", terminal_id)  # a cancelled command is ended, not left running
            raise
        if timed_out:
            self._cleanup("terminal/kill", terminal_id)
        try:
            out = self._call("terminal/output", {"terminalId": terminal_id}, timeout_s=CLIENT_CLEANUP_TIMEOUT_S)
        except ClientError:
            out = {}  # output the editor cannot report is empty; the run itself still has its result
        if not isinstance(out, dict):
            out = {}
        if exit_code is None:
            exit_code = _exit_code(out.get("exitStatus"))
        text = out.get("output") if isinstance(out.get("output"), str) else ""
        return CommandResult(exit_code=exit_code, output=text, truncated=out.get("truncated") is True,
                             timed_out=timed_out)

    def _cleanup(self, method: str, terminal_id: str) -> None:
        try:
            self._call(method, {"terminalId": terminal_id}, timeout_s=CLIENT_CLEANUP_TIMEOUT_S)
        except ClientError:
            pass  # the editor may have ended the terminal already; there is nothing more to do


def _exit_code(value: Any) -> int | None:
    code = value.get("exitCode") if isinstance(value, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


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
                 log: Callable[[str], None], store: sessions.SessionStore | None = None,
                 config_file: Path | None = None) -> None:
        self._settings = settings
        self._client = client
        self._reader = reader
        self._writer = writer
        self._log = log
        # Saved sessions (T17) and the config file the settings editor writes (default: the per-user one).
        self._store = store if store is not None else sessions.SessionStore(sessions.sessions_folder(), log=log)
        self._config_file = config_file if config_file is not None else config_module.config_path()
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()  # guards sessions, pending requests, the initialized flag and busy flags
        self._sessions: dict[str, _Session] = {}
        self._pending: dict[str, _Pending] = {}
        self._abandoned: set[str] = set()  # client requests that gave up: their late answers are dropped
        self._client_caps: dict = {}       # the client's clientCapabilities from initialize (T20)
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
        with self._lock:  # the connection is over: no late answer can come for the requests given up on
            self._abandoned.clear()

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
            "session/list": self._list_sessions,
            "session/load": self._load_session,
            "session/set_config_option": self._set_config_option,
            EXT_MODELS: self._models,
            EXT_SETTINGS_GET: self._settings_get,
            EXT_SETTINGS_SET: self._settings_set,
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
            if response_id in self._abandoned:  # a client call that already gave up: its late answer is dropped
                self._abandoned.discard(response_id)
                return
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
        pending.result = result
        pending.has_result = "result" in message
        pending.error = message.get("error") if isinstance(message.get("error"), dict) else None
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
        caps = params.get("clientCapabilities")
        with self._lock:
            self._initialized = True
            self._client_caps = caps if isinstance(caps, dict) else {}
        self._send(_result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "sessionCapabilities": {"list": {}},
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
            },
            "authMethods": [],
            "agentInfo": {"name": "wildcat-code-agent", "version": _agent_version()},
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
        # The session is saved after its first turn (an empty session is not listed). The model choices come
        # from Ollama; if it is down, the answer still comes, with a warning.
        self._send(_result(request_id, {"sessionId": session.session_id,
                                        **self._options_result(session.settings)}))

    def _prompt(self, request_id: Any, params: dict) -> None:
        self._require_initialized()
        session = self._session_of(params, required=True)
        prompt = _parse_prompt(params.get("prompt"))
        with self._lock:
            if session.busy:
                raise ProtocolError(INVALID_REQUEST, "A prompt is already running in this session.")
            session.busy = True
            session.turn += 1
            turn = session.turn
            session.cancel.clear()
            worker = threading.Thread(target=self._run_turn, args=(session, request_id, turn, prompt),
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

    # ----- saved sessions (T17) --------------------------------------------------------------------

    def _list_sessions(self, request_id: Any, params: dict) -> None:
        self._require_initialized()
        cwd = _optional_abs_path(params.get("cwd"), "cwd")
        cursor = params.get("cursor")
        if cursor is None:
            offset = 0
        elif isinstance(cursor, str) and cursor.isdigit():
            offset = int(cursor)
        else:
            raise ProtocolError(INVALID_PARAMS, "Invalid params: cursor is not one returned by session/list.")
        items = [s for s in self._store.list_sessions() if cwd is None or _same_folder(s.cwd, cwd)]
        page = items[offset:offset + LIST_PAGE_SIZE]
        sessions_out = []
        for item in page:
            entry: dict[str, Any] = {"sessionId": item.session_id, "cwd": item.cwd, "updatedAt": item.updated,
                                     "_meta": {"model": item.model}}
            if item.title:
                entry["title"] = item.title
            sessions_out.append(entry)
        result: dict[str, Any] = {"sessions": sessions_out}
        if offset + LIST_PAGE_SIZE < len(items):
            result["nextCursor"] = str(offset + LIST_PAGE_SIZE)
        self._send(_result(request_id, result))

    def _load_session(self, request_id: Any, params: dict) -> None:
        self._require_initialized()
        sid = params.get("sessionId")
        if not isinstance(sid, str):
            raise ProtocolError(INVALID_PARAMS, "Invalid params: sessionId must be a string.")
        asked_cwd = _optional_abs_path(params.get("cwd"), "cwd")
        with self._lock:
            live = self._sessions.get(sid)
            if live is not None and live.busy:
                raise ProtocolError(INVALID_REQUEST, "A prompt is already running in this session.")
        if live is not None:
            session = live  # still in this process: its history is in memory and is the same as its file
        else:
            session = self._restore_session(sid, asked_cwd)
        self._replay(session)
        self._send(_result(request_id, self._options_result(session.settings)))

    def _restore_session(self, sid: str, asked_cwd: str | None) -> _Session:
        try:
            record = self._store.load(sid)
        except sessions.SessionError:
            raise ProtocolError(RESOURCE_NOT_FOUND, "Session not found.", {"sessionId": sid}) from None
        folder = Path(record.cwd)
        if not folder.is_dir():
            raise ProtocolError(INVALID_PARAMS, f"Invalid params: the session's folder no longer exists: {record.cwd}. "
                                                "The session cannot be opened here.")
        if asked_cwd is not None and not _same_folder(record.cwd, asked_cwd):
            raise ProtocolError(INVALID_PARAMS, "Invalid params: cwd is not the session's saved folder.")
        session = _Session(session_id=sid,
                           settings=dataclasses.replace(self._settings, workspace=folder, model=record.model),
                           messages=record.messages, created=record.created)
        with self._lock:
            existing = self._sessions.get(sid)
            if existing is not None:
                return existing
            if len(self._sessions) >= MAX_SESSIONS:
                raise ProtocolError(INVALID_REQUEST, f"Too many sessions: this server keeps at most {MAX_SESSIONS} "
                                                     "per process. Start another server for more.")
            self._sessions[sid] = session
        return session

    def _replay(self, session: _Session) -> None:
        """Send the saved history as session/update notifications, the way a live turn shows it."""
        sid = session.session_id
        open_calls: list[str] = []  # tool call ids waiting for their result, in order
        count = 0
        for message in session.messages:
            role = message.get("role")
            if role == "user":
                shown = attachments.display_text(message.get("content") or "")
                self.notify_update(sid, {"sessionUpdate": "user_message_chunk", "content": _text(shown),
                                         "messageId": uuid.uuid4().hex})
            elif role == "assistant":
                if message.get("content"):
                    self.notify_update(sid, {"sessionUpdate": "agent_message_chunk",
                                             "content": _text(message["content"]), "messageId": uuid.uuid4().hex})
                for call in message.get("tool_calls") or []:
                    count += 1
                    call_id = f"r{count}"
                    name = call["function"]["name"]
                    args = dict(call["function"].get("arguments") or {})
                    tool = TOOLS.get(name)
                    open_calls.append(call_id)
                    self.notify_update(sid, {"sessionUpdate": "tool_call", "toolCallId": call_id,
                                             "title": loop._call_title(name, args),
                                             "kind": tool.kind if tool else "other", "status": "pending",
                                             "rawInput": args})
            elif role == "tool" and open_calls:
                call_id = open_calls.pop(0)
                status = "failed" if message.get("content") == loop.CANCELLED_MESSAGE else "completed"
                self.notify_update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                                         "status": status,
                                         "content": [{"type": "content", "content": _text(message.get("content") or "")}]})

    def _save_session(self, session: _Session) -> list[str]:
        """Save the session's history after a turn. A failed save does not end the chat: it is logged and
        returned as a warning, which the prompt answer carries in _meta.warnings so the client can show it."""
        if not any(isinstance(m, dict) and m.get("role") == "user" for m in session.messages):
            return []
        try:
            self._store.save(session_id=session.session_id, cwd=str(session.settings.workspace),
                             model=session.settings.model, created=session.created, messages=session.messages)
            return []
        except (sessions.SessionError, OSError) as e:
            message = f"This chat was not saved: {e}. Its later messages are not kept after a restart."
            self._log(f"Warning: session {session.session_id} was not saved: {e}")
            return [message]

    # ----- models (T17) ---------------------------------------------------------------------------

    def _bounded(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run one Ollama call with OLLAMA_CALL_TIMEOUT_S as its limit. A call that does not answer in time is a
        ConnectionError (the message then says Ollama cannot be reached)."""
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = fn(*args)
            except BaseException as e:  # handed back to the caller's thread below
                box["error"] = e

        worker = threading.Thread(target=run, name="ollama-call", daemon=True)
        worker.start()
        worker.join(OLLAMA_CALL_TIMEOUT_S)
        if worker.is_alive():
            raise ConnectionError(f"Ollama did not answer within {OLLAMA_CALL_TIMEOUT_S:g} s")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _model_names(self) -> list[str]:
        response = self._bounded(self._client.list)
        return sorted({m.model for m in response.models if m.model})

    def _tools_status(self, model: str) -> bool | None:
        """True or False when Ollama reports the model's capabilities, None when it reports none."""
        capabilities = self._bounded(self._client.show, model).capabilities
        if not capabilities:
            return None
        return "tools" in capabilities

    def _options_result(self, settings: Settings, *, names: list[str] | None = None) -> dict:
        """The session setup answer: the model option (its choices from Ollama) and warnings in _meta.

        Ollama down: the option holds only the current model, and the warning says why. A model without the
        tools capability gets a warning too, so the UI can show it (T04)."""
        warnings: list[str] = []
        current = settings.model
        status: bool | None = None
        choices = names
        try:
            if choices is None:
                choices = self._model_names()
            status = self._tools_status(current)
        except OLLAMA_ERRORS as e:
            warnings.append(describe_error(e, settings))
        if status is False:
            warnings.append(_tools_warning(current))
        options = sorted(set(choices or []) | {current})
        model_option = {"id": MODEL_OPTION_ID, "name": "Model", "category": "model", "type": "select",
                        "currentValue": current, "options": [{"value": name, "name": name} for name in options]}
        meta: dict[str, Any] = {"model": {"name": current, "tools": status}}
        if warnings:
            meta["warnings"] = warnings
        return {"configOptions": [model_option], "_meta": meta}

    def _set_config_option(self, request_id: Any, params: dict) -> None:
        self._require_initialized()
        session = self._session_of(params, required=True)
        if params.get("configId") != MODEL_OPTION_ID:
            raise ProtocolError(INVALID_PARAMS, "Invalid params: configId must be \"model\" (the only option).")
        value = params.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ProtocolError(INVALID_PARAMS, "Invalid params: value must be a model name.")
        model = value.strip()
        with self._lock:
            if session.busy:
                raise ProtocolError(INVALID_REQUEST, "A prompt is already running in this session.")
        try:
            self._tools_status(model)  # show() also proves the model is installed: it fails otherwise
        except OLLAMA_ERRORS as e:
            message = describe_error(e, dataclasses.replace(session.settings, model=model))
            if isinstance(e, ollama.ResponseError) and e.status_code == 404:
                raise ProtocolError(INVALID_PARAMS, message) from None
            raise ProtocolError(INTERNAL_ERROR, message, {"kind": "ollama"}) from None
        with self._lock:
            if session.busy:
                raise ProtocolError(INVALID_REQUEST, "A prompt is already running in this session.")
            session.settings = dataclasses.replace(session.settings, model=model)
        self._send(_result(request_id, self._options_result(session.settings)))

    def _models(self, request_id: Any, params: dict) -> None:
        self._require_initialized()
        session = self._session_of(params)
        current = session.settings.model if session is not None else self._settings.model
        try:
            names = self._model_names()
        except OLLAMA_ERRORS as e:
            self._send(_result(request_id, {"current": current, "models": [], "error": describe_error(e, self._settings)}))
            return
        self._send(_result(request_id, {"current": current, "models": names, "error": None}))

    # ----- settings (T17) -------------------------------------------------------------------------

    def _settings_get(self, request_id: Any, params: dict) -> None:
        self._require_initialized()
        self._send(_result(request_id, self._settings_result()))

    def _settings_set(self, request_id: Any, params: dict) -> None:
        self._require_initialized()
        values = params.get("values")
        if not isinstance(values, dict):
            raise ProtocolError(INVALID_PARAMS, "Invalid params: values must be an object of setting names and values.")
        try:
            before = config_write.read_editable(self._config_file)
        except ConfigError as e:
            raise ProtocolError(INVALID_PARAMS, str(e)) from None
        try:
            after = config_write.write_editable(self._config_file, values)
        except ConfigError as e:
            raise ProtocolError(INVALID_PARAMS, f"Invalid params: {e}") from None
        except OSError as e:
            raise ProtocolError(INTERNAL_ERROR, f"Cannot write the config file: {e}") from None
        if before["host"] != after["host"]:
            self._log(f"Note: the Ollama address in the config file changed to {after['host']}; "
                      "it takes effect when wildcat-code-agent starts again.")
        self._send(_result(request_id, self._settings_result(saved=True)))

    def _settings_result(self, *, saved: bool = False) -> dict:
        try:
            values = config_write.read_editable(self._config_file)
        except ConfigError as e:
            raise ProtocolError(INVALID_PARAMS, str(e)) from None
        result: dict[str, Any] = {"file": str(self._config_file), "exists": self._config_file.exists(),
                                  "values": values, "kinds": dict(config_write.EDITABLE), "applies": "restart"}
        if saved:
            result["saved"] = True
        return result

    # ----- the turn --------------------------------------------------------------------------------

    def _run_turn(self, session: _Session, request_id: Any, turn: int, prompt: attachments.Prompt) -> None:
        events = _TurnEvents(self, session, turn)
        approver = _ProtocolApprover(self, session, turn)
        client_tools = _SessionClient(self, session, self._client_caps)
        response: dict
        warnings: list[str] = []
        try:
            # The attached context (T21) is in the user message for this turn only. Later turns and the saved
            # history keep the typed text and a marker listing the attachments (see attachments.history_text).
            content = attachments.build_message(prompt, workspace=session.settings.workspace,
                                                num_ctx=session.settings.num_ctx, client=client_tools)
            session.messages.append({"role": "user", "content": content})
            index = len(session.messages) - 1
            try:
                loop.run_turn(session.messages, client=self._client, settings=session.settings, emit=events,
                              approver=approver, cancel=session.cancel, session=session.approvals,
                              client_tools=client_tools)
            finally:
                if prompt.attachments:
                    session.messages[index]["content"] = attachments.history_text(
                        prompt, workspace=session.settings.workspace)
            warnings = self._save_session(session)  # before the answer, so a session/list that follows shows it
            outcome = events.outcome
            if isinstance(outcome, TurnDone):
                response = _result(request_id, _prompt_result(outcome, warnings))
            elif isinstance(outcome, Error):
                data: dict[str, Any] = {"kind": "agent"}
                if warnings:
                    data["warnings"] = warnings
                response = _error(request_id, INTERNAL_ERROR, outcome.message, data)
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
        tool_call: dict[str, Any] = {"toolCallId": f"t{turn}-{request.tool_call_id}", "title": request.title,
                                     "status": "pending", "rawInput": request.arguments}
        if request.preview is not None:  # the diff or the proposed content the user approves: ACP's _meta extension point
            tool_call["_meta"] = {"preview": {"kind": request.preview.kind, "text": request.preview.text,
                                              "path": request.preview.path, "shell": request.preview.shell}}
        self._send(_request(request_id, "session/request_permission", {
            "sessionId": session.session_id,
            "toolCall": tool_call,
            "options": options,
        }))
        return request_id, pending

    def finish_permission_request(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def call_client(self, session: _Session, method: str, params: dict, *, timeout_s: float,
                    cancellable: bool = False) -> Any:
        """Send a request to the client (fs/* or terminal/*, T20) and wait for its answer; returns the result.

        Raises ClientError for an error answer, and for no answer within timeout_s (timed_out=True). With
        `cancellable`, a cancel of the session while waiting raises ToolCancelled. A request that gave up is
        remembered in _abandoned, so its answer, if it still comes, is dropped without a warning.
        """
        with self._lock:
            self._request_count += 1
            request_id = f"srv-{self._request_count}"
            pending = _Pending()
            self._pending[request_id] = pending
        self._send(_request(request_id, method, {"sessionId": session.session_id, **params}))
        deadline = time.monotonic() + timeout_s
        answered = False
        try:
            while not pending.event.wait(_POLL_S):
                if cancellable and session.cancel.is_set():
                    raise ToolCancelled()
                if time.monotonic() >= deadline:
                    raise ClientError(f"the editor did not answer {method} within {timeout_s:g} s", timed_out=True)
            answered = True
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
                if not answered:
                    self._abandoned.add(request_id)
        if pending.error is not None:
            code = pending.error.get("code")
            message = pending.error.get("message")
            raise ClientError(str(message) if message else "the editor reported an error",
                              code=code if isinstance(code, int) and not isinstance(code, bool) else None)
        if not pending.has_result:  # neither a result nor an error object: not an answer, so not a success
            raise ClientError(f"the editor sent a malformed answer to {method}")
        return pending.result

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
            except TransportClosed as e:  # the WebSocket connection closed (web transport)
                self._writes_failed = True
                self._log(f"Warning: the client connection closed; no more messages are sent ({e}).")
            except (OSError, ValueError) as e:  # the client has gone away: nothing more can be sent
                self._writes_failed = True
                self._log(f"Warning: cannot write to stdout ({type(e).__name__}: {e}); no more messages are sent.")


def _parse_prompt(blocks: Any) -> attachments.Prompt:
    """The text and attachments of a session/prompt (T21). Text blocks, resource_link and embedded text
    resources are accepted; anything else is -32602. Plain text prompts are unchanged."""
    try:
        return attachments.parse_prompt(blocks)
    except attachments.PromptError as e:
        raise ProtocolError(INVALID_PARAMS, str(e)) from None


def _prompt_result(done: TurnDone, warnings: list[str] | None = None) -> dict:
    result: dict[str, Any] = {"stopReason": done.stop_reason}
    meta: dict[str, Any] = {}
    if done.usage is not None:  # ACP has no token counts in the prompt result, so they go under _meta
        meta["usage"] = {"promptTokens": done.usage.prompt_tokens, "completionTokens": done.usage.completion_tokens}
    if warnings:  # e.g. the chat could not be saved; the client shows them to the user
        meta["warnings"] = list(warnings)
    if meta:
        result["_meta"] = meta
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


@contextlib.contextmanager
def running_agent(settings: Settings, *, client: Any, log: Callable[[str], None]) -> Iterator[None]:
    """What every transport needs around its server: plugins and MCP servers start first, the startup
    model check runs beside the server, and everything closes last. Shared by stdio and the web server."""
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
        yield
    finally:
        if probe_thread is not None:  # let a quick model check finish, so its warning is not lost
            probe_thread.join(PROBE_JOIN_S)
        if mcp is not None:
            mcp.close()
        if plugin_runtime is not None:
            plugin_runtime.close()


def serve_stdio(settings: Settings, *, client: Any = None, reader: Iterable[str] | None = None,
                writer: TextIO | None = None, errors: TextIO | None = None,
                store: sessions.SessionStore | None = None, config_file: Path | None = None) -> None:
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
        owned_writer = io.TextIOWrapper(os.fdopen(owned_fd, "wb"), encoding="utf-8", newline="\n",
                                        write_through=True)
        writer = owned_writer
    if reader is None:
        reader = _bounded_lines(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"), MAX_LINE_CHARS)
    if client is None:
        client = ollama.Client(host=settings.host)

    try:
        with contextlib.redirect_stdout(errors), running_agent(settings, client=client, log=log):
            try:
                StdioServer(settings, client=client, reader=reader, writer=writer, log=log, store=store,
                            config_file=config_file).serve()
            except KeyboardInterrupt:  # Ctrl+C ends the server like EOF does
                pass
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
