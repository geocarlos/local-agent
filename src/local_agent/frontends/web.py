"""The WebSocket transport of the ACP server: `local-agent serve --web` (T15).

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
from typing import Any, TextIO

import ollama
import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse

from local_agent.config import Settings
from local_agent.frontends import server as acp

WS_PATH = "/ws"
TOKEN_PARAM = "token"
WS_POLICY_VIOLATION = 1008  # the close code sent (before accept) when a connection is refused
LISTEN_BACKLOG = 16


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


def presented_token(websocket: WebSocket) -> str | None:
    """The token a client sent: the Bearer header if there is an Authorization header, else the query parameter."""
    auth = websocket.headers.get("authorization")
    if auth is not None:
        scheme, _, value = auth.partition(" ")
        return value.strip() if scheme.lower() == "bearer" else None
    return websocket.query_params.get(TOKEN_PARAM)


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
                raise ValueError("the connection is closed")
            try:
                self._loop.call_soon_threadsafe(self._outbox.put_nowait, line)
            except RuntimeError:  # the event loop has stopped
                raise ValueError("the event loop is closed") from None
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
                            log: Callable[[str], None], threads: list[threading.Thread]) -> None:
    """Run one accepted WebSocket as one ACP server until the client disconnects."""
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue = asyncio.Queue()
    writer = _FrameWriter(loop, outbox)
    reader = _FrameReader()
    agent = acp.StdioServer(settings, client=client, reader=reader, writer=writer, log=log)
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
               threads: list[threading.Thread] | None = None) -> FastAPI:
    """The FastAPI app: a placeholder page at `/` and the ACP WebSocket at `/ws`.

    `port` is the port the server is bound to (the Origin check needs it). `threads` collects the
    connection threads, so the caller can wait for them at shutdown. The OpenAPI and docs routes are off.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    connection_threads: list[threading.Thread] = [] if threads is None else threads

    @app.get("/")
    async def index() -> PlainTextResponse:
        # The web UI is a later task (T16); until then this page only says where the WebSocket is.
        return PlainTextResponse("local-agent web server. The agent WebSocket is at /ws and needs the token.\n")

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
        await _serve_connection(websocket, settings=settings, client=client, log=log, threads=connection_threads)

    return app


def bind_loopback(port: int) -> socket.socket:
    """A listening socket on 127.0.0.1. Port 0 takes a free port from the system."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(LISTEN_BACKLOG)
    except OSError:
        sock.close()
        raise
    return sock


def serve_web(settings: Settings, *, port: int = 0, client: Any = None, errors: TextIO | None = None) -> None:
    """Run the web server until it is stopped (Ctrl+C). The URL and the token are printed to stderr.

    Plugins and MCP servers start before the server accepts connections and close after the last
    connection's turns have stopped. Nothing is written to stdout except what plugins print, which goes
    to stderr as on stdio.
    """
    errors = sys.stderr if errors is None else errors

    def log(line: str) -> None:
        print(line, file=errors, flush=True)

    try:
        sock = bind_loopback(port)
    except OSError as e:
        log(f"Error: cannot listen on 127.0.0.1:{port}: {e}")
        raise SystemExit(1) from None
    bound_port = sock.getsockname()[1]
    token = new_token()
    if client is None:
        client = ollama.Client(host=settings.host)
    threads: list[threading.Thread] = []
    app = create_app(settings, token=token, port=bound_port, client=client, log=log, threads=threads)
    config = uvicorn.Config(app, ws="websockets", access_log=False, log_level="warning", lifespan="off")
    uv_server = uvicorn.Server(config)
    try:
        with contextlib.redirect_stdout(errors), acp.running_agent(settings, client=client, log=log):
            try:
                log(f"local-agent web server: http://127.0.0.1:{bound_port}/")
                log(f"WebSocket: ws://127.0.0.1:{bound_port}{WS_PATH}?{TOKEN_PARAM}={token}")
                log("The token is new on every start. Anyone who has it can make the agent act as you: keep it private.")
                uv_server.run(sockets=[sock])
            except KeyboardInterrupt:  # Ctrl+C stops the server like it stops the stdio server
                pass
            finally:
                # Each connection cancels its turns when its socket closes; wait for them, with one deadline.
                end = time.monotonic() + acp.SHUTDOWN_DEADLINE_S
                for thread in threads:
                    thread.join(max(0.0, end - time.monotonic()))
    finally:
        with contextlib.suppress(OSError):
            sock.close()
