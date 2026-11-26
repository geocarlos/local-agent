"""MCP client runtime: the stdio servers run on one background thread with its own asyncio loop.

The agent loop is synchronous and the mcp SDK is asyncio, so the sync side talks to this
hub through asyncio.run_coroutine_threadsafe. Each server runs in its own task on the
hub's loop. That task enters the SDK client and leaves it (the SDK needs both in one task),
lists the server's tools, and then waits until close(). Calls from other tasks use the same
client.

Waits on the sync side poll every POLL_S seconds, so Ctrl+C (KeyboardInterrupt) and a cancel
event are seen promptly, and a call is cancelled when its time limit passes.

A server's stderr goes to a file per server (not the terminal), so a server that fails to
start can show its last lines in the warning.
"""

import asyncio
import concurrent.futures
import os
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult
from mcp.types import Tool as McpTool

POLL_S = 0.1              # how often a waiting call looks for Ctrl+C or a cancel
SHUTDOWN_S = 5.0          # how long close() gives the servers to exit on their own
STDERR_TAIL_CHARS = 500   # how much of a server's stderr a start failure shows
STDERR_TAIL_BYTES = 4096  # the end of the stderr file that is read for it (at least STDERR_TAIL_CHARS of text)


class CallFailed(Exception):
    """A call could not finish: the time limit passed, the server is not running, or it ended."""


class CallCancelled(Exception):
    """The call was cancelled (the cancel event was set) before it finished."""


class _HubTimeout(Exception):
    """Internal: a wait on the hub's loop passed its time limit."""


@dataclass(frozen=True)
class ServerSpec:
    """A server to start: its name (as in the config) and how to launch it."""

    name: str
    params: StdioServerParameters


@dataclass(frozen=True)
class ServerStart:
    """The outcome of starting one server: its tools, or the reason it did not start."""

    name: str
    tools: tuple[McpTool, ...] = ()
    error: str | None = None


class McpHub:
    """Owns the background loop and the server sessions. Use from one thread (the agent's)."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-hub", daemon=True)
        self._thread.start()
        self._stop: asyncio.Event | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._clients: dict[str, Client] = {}
        self._stderr_paths: dict[str, Path] = {}
        self._stderr_files: dict[str, TextIO] = {}
        self._closed = False

    # --- sync API (called from the agent's thread) -------------------------------------------

    def start_servers(self, specs: Sequence[ServerSpec], timeout_s: float) -> list[ServerStart]:
        """Start the servers together and list their tools. A server that does not answer in time is stopped.

        Returns one ServerStart per spec, in order. Nothing is raised for a failing server.
        """
        for spec in specs:
            handle, path = tempfile.mkstemp(prefix=f"mcp-{spec.name}-", suffix=".log")
            os.close(handle)
            self._stderr_paths[spec.name] = Path(path)
            self._stderr_files[spec.name] = open(path, "w", encoding="utf-8", errors="replace")
        return self._await(self._start_all(list(specs), timeout_s))

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any], *, timeout_s: float,
                  cancel: threading.Event | None = None) -> CallToolResult:
        """Call one tool and wait for its result. Raises CallFailed or CallCancelled; KeyboardInterrupt propagates."""
        client = self._clients.get(server)
        if client is None:
            raise CallFailed(f"MCP server '{server}' is not running")
        try:
            return self._await(client.call_tool(tool, arguments), timeout_s=timeout_s, cancel=cancel)
        except _HubTimeout:
            raise CallFailed(f"timed out after {timeout_s:g} s and was cancelled") from None
        except (CallFailed, CallCancelled):
            raise
        except Exception as e:  # the server ended (for example a closed pipe) or answered with a protocol error
            raise CallFailed(f"the server failed ({e})") from e

    def close(self) -> None:
        """Stop every server (its process too) and the background thread. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._stop is not None:
                self._await(self._shutdown(), timeout_s=SHUTDOWN_S * 3)
        except (_HubTimeout, Exception):
            pass  # a server that will not stop is left to the OS; the thread is a daemon
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(SHUTDOWN_S)
        if not self._thread.is_alive():
            self._loop.close()
        for handle in self._stderr_files.values():
            handle.close()
        for path in self._stderr_paths.values():
            try:
                path.unlink()
            except OSError:
                pass  # still open by a stuck server; the temp file is left behind

    def stderr_tail(self, name: str) -> str:
        """The last characters a server wrote to stderr (empty if none, or the file is gone)."""
        path = self._stderr_paths.get(name)
        if path is None:
            return ""
        try:  # only the end of the file is read, so a server that wrote a lot is not loaded whole
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - STDERR_TAIL_BYTES))
                data = handle.read()
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace").strip()[-STDERR_TAIL_CHARS:]

    # --- helpers ------------------------------------------------------------------------------

    def _await(self, coro: Any, *, timeout_s: float | None = None, cancel: threading.Event | None = None) -> Any:
        """Run a coroutine on the hub's loop and wait for its result, polling for Ctrl+C, cancel and the limit."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        try:
            while True:
                done, _ = concurrent.futures.wait([future], timeout=POLL_S)
                if done:
                    return future.result()
                if cancel is not None and cancel.is_set():
                    raise CallCancelled()
                if deadline is not None and time.monotonic() >= deadline:
                    raise _HubTimeout()
        except BaseException:  # KeyboardInterrupt, cancel and timeout: stop the coroutine, then pass it on
            future.cancel()
            raise

    async def _start_all(self, specs: list[ServerSpec], timeout_s: float) -> list[ServerStart]:
        if not specs:
            return []
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        readies: dict[str, asyncio.Future[tuple[McpTool, ...]]] = {}
        try:
            for spec in specs:
                ready: asyncio.Future[tuple[McpTool, ...]] = loop.create_future()
                readies[spec.name] = ready
                self._tasks[spec.name] = loop.create_task(self._serve(spec, ready))
            await asyncio.wait(list(readies.values()), timeout=timeout_s)
        except BaseException:  # for example Ctrl+C while waiting: stop the servers that were started
            for task in self._tasks.values():
                task.cancel()
            raise
        starts: list[ServerStart] = []
        late: list[asyncio.Task[None]] = []
        for spec in specs:
            ready = readies[spec.name]
            if not ready.done():
                late.append(self._tasks[spec.name])
                starts.append(ServerStart(spec.name, error=f"did not answer within {timeout_s:g} s"))
            elif ready.exception() is not None:
                starts.append(ServerStart(spec.name, error=self._start_error(spec.name, ready.exception())))
            else:
                starts.append(ServerStart(spec.name, tools=ready.result()))
        for task in late:  # a server that never answered is stopped, so its process does not linger
            task.cancel()
        if late:
            await asyncio.wait(late, timeout=SHUTDOWN_S)
        return starts

    def _start_error(self, name: str, error: BaseException | None) -> str:
        text = (str(error).strip() or type(error).__name__) if error is not None else "unknown error"
        tail = self.stderr_tail(name)
        return f"{text}. Server output: {tail}" if tail else text

    async def _serve(self, spec: ServerSpec, ready: asyncio.Future[tuple[McpTool, ...]]) -> None:
        stderr = self._stderr_files[spec.name]
        try:
            async with Client(stdio_client(spec.params, errlog=stderr)) as client:
                self._clients[spec.name] = client
                tools = await _list_all(client)
                if not ready.done():
                    ready.set_result(tuple(tools))
                assert self._stop is not None
                await self._stop.wait()
        except Exception as e:  # the start failed (or the exit did); a failed start is reported to the caller
            if not ready.done():
                ready.set_exception(e)
        finally:
            self._clients.pop(spec.name, None)

    async def _shutdown(self) -> None:
        if self._stop is not None:
            self._stop.set()
        tasks = list(self._tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_S)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=SHUTDOWN_S)


async def _list_all(client: Client) -> list[McpTool]:
    """Every tool the server lists, following the pagination cursor."""
    tools: list[McpTool] = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools
