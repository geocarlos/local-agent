"""The WebSocket transport of the ACP server: `wildcat-code-agent serve --web` (T15).

Each WebSocket connection is one `StdioServer` (frontends/server.py), so a session, its turns and its
approvals behave exactly as on stdio. A text frame is one JSON-RPC message, the same line that stdio
carries without its `\\n`. Plugins, MCP servers and the model check start once per process (shared by
all connections) through `server.running_agent`.

Security (decided, see docs/protocol.md and TASKS.md T15):
- The server binds 127.0.0.1 only. There is no option to bind another address.
- Every process makes a new random token (`secrets.token_urlsafe`). A WebSocket must present it, either
  as the `token` query parameter (browsers cannot set headers on a WebSocket) or as an
  `Authorization: Bearer <token>` header (scripts and editor extensions). It is compared with
  `hmac.compare_digest`. A missing or wrong token is refused.
- A connection that sends an `Origin` header must send one of this server's own origins,
  `http://127.0.0.1:<port>` or `http://localhost:<port>`. Browsers always send `Origin` on a WebSocket,
  so a page on another site is refused even if it somehow has the token. A connection with no `Origin`
  is a non-browser client; it is allowed, because the token is still required.
- Refusals happen before the WebSocket is accepted, so the handshake gets HTTP 403. The token is never
  written to a log: uvicorn's access log is off, and the server's own messages do not name the URL.
"""

import asyncio
import contextlib
import hmac
import queue
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TextIO

import ollama
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from wildcat_code_agent import config, sessions
from wildcat_code_agent.config import Settings
from wildcat_code_agent.frontends import server as acp
from wildcat_code_agent.frontends import webassets

WS_PATH = "/ws"
TOKEN_PARAM = "token"
WS_POLICY_VIOLATION = 1008  # the close code sent (before accept) when a connection is refused
LISTEN_BACKLOG = 16
# The largest WebSocket message the transport accepts, in UTF-8 bytes. A line of MAX_LINE_CHARS characters
# is at most 4 bytes per character, so every such line reaches the server, which answers -32600 when it is
# longer than the character limit. A larger message is closed by the WebSocket layer (code 1009).
WS_MAX_MESSAGE_BYTES = 4 * acp.MAX_LINE_CHARS


def new_token() -> str:
    """A random token for one process run: 32 bytes, URL-safe."""
    return secrets.token_urlsafe(32)


def allowed_origins(port: int) -> frozenset[str]:
    """The only origins that may open a WebSocket to this server."""
    return frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})


def origin_ok(origin: str | None, port: int) -> bool:
    """True when the Origin header is absent (a non-browser client) or is one of this server's own origins."""
    return origin is None or origin in allowed_origins(port)


def token_ok(presented: str | None, expected: str) -> bool:
    """True when the presented token equals the expected one. The comparison takes the same time for any
    content of the same length, so it does not leak the token one character at a time."""
    if presented is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8", "replace"), expected.encode("utf-8"))


def bearer_token(request: Request) -> str | None:
    """The token of an `Authorization: Bearer` header, or None. The query parameter is not read (the /info route)."""
    auth = request.headers.get("authorization")
    if auth is None:
        return None
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else None


def presented_token(request: Request | WebSocket) -> str | None:
    """The token a client sent: the Bearer header if there is an Authorization header, else the query parameter."""
    auth = request.headers.get("authorization")
    if auth is not None:
        scheme, _, value = auth.partition(" ")
        return value.strip() if scheme.lower() == "bearer" else None
    return request.query_params.get(TOKEN_PARAM)


class _FrameWriter:
    """The writer a StdioServer sees: each protocol line becomes one outgoing WebSocket text frame.

    The server's threads call `write`; the frames are handed to the event loop, which sends them."""

    def __init__(self, loop: asyncio.AbstractEventLoop, outbox: asyncio.Queue) -> None:
        self._loop = loop
        self._outbox = outbox
        self._lock = threading.Lock()
        self._closed = False

    def write(self, text: str) -> int:
        line = text[:-1] if text.endswith("\n") else text
        with self._lock:
            if self._closed:
                raise acp.TransportClosed("the WebSocket connection is closed")
            try:
                self._loop.call_soon_threadsafe(self._outbox.put_nowait, line)
            except RuntimeError:  # the event loop has stopped: the server has shut down
                raise acp.TransportClosed("the server is shutting down") from None
        return len(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self._lock:
            self._closed = True


class _FrameReader:
    """The reader a StdioServer sees: lines put by the receive loop. `None` is end of input."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def put(self, item: str | object | None) -> None:
        self._queue.put(item)

    def __iter__(self) -> Iterator[Any]:
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item


async def _send_frames(websocket: WebSocket, outbox: asyncio.Queue) -> None:
    """Send each queued line as one text frame until the connection is gone."""
    try:
        while True:
            line = await outbox.get()
            await websocket.send_text(line)
    except Exception:  # the client has gone away; the receive loop ends the connection
        return


async def _serve_connection(websocket: WebSocket, *, settings: Settings, client: Any,
                            log: Callable[[str], None], threads: list[threading.Thread],
                            store: sessions.SessionStore, config_file: Path) -> None:
    """Run one accepted WebSocket as one ACP server until the client disconnects."""
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue = asyncio.Queue()
    writer = _FrameWriter(loop, outbox)
    reader = _FrameReader()
    agent = acp.StdioServer(settings, client=client, reader=reader, writer=writer, log=log, store=store,
                            config_file=config_file)
    thread = threading.Thread(target=agent.serve, name=f"ws-connection-{len(threads)}", daemon=True)
    threads[:] = [t for t in threads if t.is_alive()] + [thread]
    thread.start()
    sender = asyncio.create_task(_send_frames(websocket, outbox))
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:  # a binary frame is read as UTF-8 text, so it gets the same JSON-RPC answer
                text = (message.get("bytes") or b"").decode("utf-8", "replace")
            reader.put(acp.OVERSIZE_LINE if len(text) > acp.MAX_LINE_CHARS else text)
    finally:
        reader.put(None)  # end of input: the server cancels its running turns and returns
        writer.close()
        sender.cancel()


def create_app(settings: Settings, *, token: str, port: int, client: Any,
               log: Callable[[str], None] = lambda line: print(line, file=sys.stderr, flush=True),
               threads: list[threading.Thread] | None = None, store: sessions.SessionStore | None = None,
               config_file: Path | None = None) -> FastAPI:
    """The FastAPI app: the web UI at `/` (static files from the web folder), `/info` (needs the token; gives
    the workspace folder the UI starts sessions in) and the ACP WebSocket at `/ws`.

    `port` is the port the server is bound to (the Origin check and the CSP need it). `threads` collects the
    connection threads, so the caller can wait for them at shutdown. The OpenAPI and docs routes are off.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    connection_threads: list[threading.Thread] = [] if threads is None else threads
    if store is None:
        store = sessions.SessionStore(sessions.sessions_folder(), log=log)
    if config_file is None:
        config_file = config.config_path()
    headers = webassets.page_headers(port)

    @app.get("/info")
    async def info(request: Request) -> Response:
        no_store = {**headers, "Cache-Control": "no-store"}
        if not token_ok(bearer_token(request), token):  # the header only: a query token would reach logs and history
            return JSONResponse({"error": "A valid token is needed."}, status_code=403, headers=no_store)
        return JSONResponse({"cwd": str(settings.workspace), "protocolVersion": acp.PROTOCOL_VERSION},
                            headers=no_store)

    @app.get("/{name:path}")
    async def static_file(name: str) -> Response:
        # Only files of the web folder are served (webassets.find_asset). Anything else is a 404, including
        # paths with "..", and the docs and OpenAPI paths, which are not routed.
        path = webassets.find_asset(name)
        if path is None:
            return PlainTextResponse("Not found.\n", status_code=404, headers=headers)
        return FileResponse(path, media_type=webassets.content_type(path), headers=headers)

    @app.websocket(WS_PATH)
    async def agent_socket(websocket: WebSocket) -> None:
        if not origin_ok(websocket.headers.get("origin"), port):
            log("Warning: refused a WebSocket connection from an origin that is not this server.")
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        if not token_ok(presented_token(websocket), token):
            log("Warning: refused a WebSocket connection with a missing or wrong token.")
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        await websocket.accept()
        await _serve_connection(websocket, settings=settings, client=client, log=log, threads=connection_threads,
                                store=store, config_file=config_file)

    return app


def bind_loopback(port: int) -> socket.socket:
    """A listening socket on 127.0.0.1. Port 0 takes a free port from the system."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            # Without this, another process may bind the same port (Windows allows it for sockets that set
            # SO_REUSEADDR). Exclusive use makes a fixed --port fail loudly instead.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(LISTEN_BACKLOG)
    except OSError:
        sock.close()
        raise
    return sock


STARTUP_TIMEOUT_S = 15.0


class ServerStartError(RuntimeError):
    """The web server could not start: plugins or MCP servers failed, or uvicorn stopped before it listened."""


def _stderr_log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class WebServer:
    """The web server of `serve --web`, running in this process (T15, T18).

    It binds 127.0.0.1 on `port` (0: a free port), makes the token, and wraps the server in the plugins and
    MCP servers (`server.running_agent`). `start` runs uvicorn on a background thread and returns once it is
    listening. `stop` shuts it down: the WebSocket connections end and their turns are cancelled, then the
    plugins close, all under the same deadline as stdio (`SHUTDOWN_DEADLINE_S`). `serve_web` and the desktop
    window (`frontends/gui.py`) both use it.

    `log` receives the server's own lines. Nothing here prints the token: `serve_web` prints it on purpose.
    Raises OSError from the constructor when the port cannot be bound.
    """

    def __init__(self, settings: Settings, *, port: int = 0, client: Any = None,
                 log: Callable[[str], None] = _stderr_log, store: sessions.SessionStore | None = None,
                 config_file: Path | None = None) -> None:
        self._settings = settings
        self._log = log
        self._sock = bind_loopback(port)
        self.port: int = self._sock.getsockname()[1]
        self.token: str = new_token()
        self._client = ollama.Client(host=settings.host) if client is None else client
        self._store = sessions.SessionStore(sessions.sessions_folder(), log=log) if store is None else store
        self._threads: list[threading.Thread] = []  # one per WebSocket connection, joined at stop
        app = create_app(settings, token=self.token, port=self.port, client=self._client, log=log,
                         threads=self._threads, store=self._store,
                         config_file=config.config_path() if config_file is None else config_file)
        self._uv_server = uvicorn.Server(uvicorn.Config(app, ws="websockets-sansio", ws_max_size=WS_MAX_MESSAGE_BYTES,
                                                        access_log=False, log_level="warning", lifespan="off"))
        self._stack = contextlib.ExitStack()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._stopped = False

    @property
    def url(self) -> str:
        """The page's address without the token."""
        return f"http://127.0.0.1:{self.port}/"

    @property
    def link(self) -> str:
        """The page's address with the token in the fragment: a browser never sends a fragment to the server."""
        return f"{self.url}#{TOKEN_PARAM}={self.token}"

    @property
    def websocket_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{WS_PATH}?{TOKEN_PARAM}={self.token}"

    @property
    def error(self) -> BaseException | None:
        """What stopped uvicorn, if it stopped by an error rather than by `stop`."""
        return self._error

    def is_serving(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout_s: float = STARTUP_TIMEOUT_S) -> None:
        """Start the plugins and MCP servers, then uvicorn. Returns once the server is listening."""
        try:
            self._stack.enter_context(acp.running_agent(self._settings, client=self._client, log=self._log))
        except Exception as e:
            self._close_socket()
            raise ServerStartError(f"the plugins or MCP servers did not start: {e}") from e
        self._thread = threading.Thread(target=self._run, name="web-server", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not self._uv_server.started and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self._uv_server.started:
            self.stop()
            if self._error is not None:
                raise ServerStartError(f"the web server stopped while it started: {self._error}") from self._error
            raise ServerStartError("the web server did not start in time")

    def _run(self) -> None:
        try:
            self._uv_server.run(sockets=[self._sock])
        except BaseException as e:  # kept for start and for the caller; the thread ends
            self._error = e

    def stop(self) -> None:
        """Shut the server down: stop accepting, end the connections, wait for their threads, close the plugins.
        Safe to call more than once."""
        if self._stopped:
            return
        self._stopped = True
        end = time.monotonic() + acp.SHUTDOWN_DEADLINE_S
        self._uv_server.should_exit = True
        if self._thread is not None:
            self._thread.join(max(0.0, end - time.monotonic()))
            if self._thread.is_alive():
                self._uv_server.force_exit = True
                self._thread.join(1.0)
        for thread in self._threads:
            thread.join(max(0.0, end - time.monotonic()))
        still_running = [t for t in self._threads if t.is_alive()]
        if still_running:
            self._log(f"Warning: {len(still_running)} connection(s) did not stop within "
                      f"{acp.SHUTDOWN_DEADLINE_S:g} s; exiting anyway.")
        self._stack.close()  # plugins and MCP servers close after the connections' turns stop
        self._close_socket()

    def _close_socket(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


def serve_web(settings: Settings, *, port: int = 0, client: Any = None, errors: TextIO | None = None,
              store: sessions.SessionStore | None = None, config_file: Path | None = None) -> None:
    """Run the web server until it is stopped (Ctrl+C). The URL and the token are printed to stderr.

    Plugins and MCP servers start before the server accepts connections and close after the last
    connection's turns have stopped. Nothing is written to stdout except what plugins print, which goes
    to stderr as on stdio.
    """
    errors = sys.stderr if errors is None else errors

    def log(line: str) -> None:
        print(line, file=errors, flush=True)

    try:
        server = WebServer(settings, port=port, client=client, log=log, store=store, config_file=config_file)
    except OSError as e:
        log(f"Error: cannot listen on 127.0.0.1:{port}: {e}")
        raise SystemExit(1) from None
    with contextlib.redirect_stdout(errors):
        try:
            server.start()
        except ServerStartError as e:
            log(f"Error: {e}")
            raise SystemExit(1) from None
        try:
            log(f"wildcat-code-agent web server: {server.url}")
            # The token is in the fragment: a browser never sends it to the server, and the page removes it from the address bar.
            log(f"Open this link in a browser: {server.link}")
            log(f"WebSocket: {server.websocket_url}")
            log("The token is new on every start. Anyone who has it can make the agent act as you: keep it private.")
            while server.is_serving():
                time.sleep(0.5)  # a short wait, so Ctrl+C is seen on Windows too (a plain join is not interruptible there)
        except KeyboardInterrupt:  # Ctrl+C stops the server like it stops the stdio server
            pass
        finally:
            server.stop()
        if server.error is not None:
            log(f"Error: the web server stopped: {server.error}")
            raise SystemExit(1)
