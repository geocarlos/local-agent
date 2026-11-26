"""The stdio server (T11), driven in-process: JSON-RPC lines in and out, with the fake Ollama client.

The server runs on its own thread, reading from a queue-backed stdin. The test plays the client:
it sends request lines, reads what the server writes, and answers session/request_permission.
"""

import io
import json
import queue
import threading
import time
from pathlib import Path

import httpx
import pytest

from fakes import FakeOllama, Raise, Run, Stream, chunk, text_reply, tool_reply
from wildcat_code_agent.config import Settings
from wildcat_code_agent.frontends import server as server_module
from wildcat_code_agent.frontends.server import OVERSIZE_LINE, StdioServer, _bounded_lines, serve_stdio

TIMEOUT = 5


class _Inbox:
    """The server's stdin: lines the test puts in. Closing it is EOF."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def put(self, line: str) -> None:
        self._queue.put(line)

    def close(self) -> None:
        self._queue.put(None)

    def __iter__(self):
        while True:
            line = self._queue.get()
            if line is None:
                return
            yield line


class _Outbox:
    """The server's stdout: each write is one protocol line, kept in order for the test to read."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.lines: queue.Queue = queue.Queue()

    def write(self, text: str) -> None:
        self.writes.append(text)
        self.lines.put(text)

    def flush(self) -> None:
        pass


class Wire:
    """A server on a thread, and the client end of its streams."""

    def __init__(self, workspace: Path, responses: list, *, settings: Settings | None = None,
                 client: FakeOllama | None = None, **server_options) -> None:
        self.client = client if client is not None else FakeOllama(responses)
        self.inbox = _Inbox()
        self.outbox = _Outbox()
        self.log: list[str] = []
        self.settings = settings or Settings(workspace=workspace)
        self.server = StdioServer(self.settings, client=self.client, reader=self.inbox, writer=self.outbox,
                                  log=self.log.append, **server_options)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self._ids = 0

    def send(self, line: str) -> None:
        self.inbox.put(line)

    def request(self, method: str, params) -> int:
        self._ids += 1
        self.send(json.dumps({"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params}) + "\n")
        return self._ids

    def notify(self, method: str, params: dict) -> None:
        self.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")

    def read(self) -> dict:
        return json.loads(self.outbox.lines.get(timeout=TIMEOUT))

    def read_until(self, request_id: int, *, answer=None) -> list[dict]:
        """Read messages up to the response to `request_id`. Permission requests get `answer(request)`,
        an optionId or None (cancelled). Default: allow once."""
        messages: list[dict] = []
        while True:
            message = self.read()
            messages.append(message)
            if message.get("method") == "session/request_permission":
                choice = answer(message) if answer is not None else "allow_once"
                self.respond_permission(message["id"], choice)
            elif message.get("id") == request_id and "method" not in message:
                return messages

    def respond_permission(self, request_id, option_id: str | None) -> None:
        outcome = {"outcome": "cancelled"} if option_id is None else {"outcome": "selected", "optionId": option_id}
        self.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome}}) + "\n")

    def close(self) -> None:
        self.inbox.close()
        self.thread.join(TIMEOUT)
        assert not self.thread.is_alive(), "the server did not shut down after EOF"


def initialize(w: Wire) -> dict:
    return w.read_until(w.request("initialize", {"protocolVersion": 1}))[-1]


def new_session(w: Wire, cwd: Path) -> str:
    response = w.read_until(w.request("session/new", {"cwd": str(cwd), "mcpServers": []}))[-1]
    return response["result"]["sessionId"]


def prompt(w: Wire, session_id: str, text: str, *, answer=None) -> tuple[int, list[dict]]:
    request_id = w.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]})
    return request_id, w.read_until(request_id, answer=answer)


def updates(messages: list[dict]) -> list[dict]:
    return [m["params"]["update"] for m in messages if m.get("method") == "session/update"]


def wait_for(predicate, timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.01)


def cancel_now(w: Wire, session_id: str) -> None:
    """Send session/cancel from the client, then wait until the server has seen it."""
    w.notify("session/cancel", {"sessionId": session_id})
    wait_for(lambda: w.server._sessions[session_id].cancel.is_set())


def roles(messages: list[dict]) -> list[str]:
    return [m["role"] for m in messages]


def assert_tool_calls_answered(messages: list[dict]) -> None:
    """The T03 history invariant: every tool call of an assistant message has one tool message after it."""
    for index, message in enumerate(messages):
        if message["role"] == "assistant" and message.get("tool_calls"):
            answers = messages[index + 1:index + 1 + len(message["tool_calls"])]
            assert [a["role"] for a in answers] == ["tool"] * len(message["tool_calls"])


# ----- the full turn -----------------------------------------------------------------------------------


def test_full_turn_with_an_approved_write_over_the_protocol(workspace):
    w = Wire(workspace, [
        tool_reply(("write_file", {"path": "out.txt", "content": "hello"})),
        text_reply("saved", prompt_eval_count=40, eval_count=7),
    ])
    init = initialize(w)
    assert init["result"]["protocolVersion"] == 1
    assert init["result"]["agentCapabilities"]["promptCapabilities"]["image"] is False
    session_id = new_session(w, workspace)

    asked: list[dict] = []

    def answer(request: dict) -> str:
        asked.append(request)
        return "allow_once"

    request_id, messages = prompt(w, session_id, "write hello", answer=answer)

    assert len(asked) == 1
    params = asked[0]["params"]
    assert params["sessionId"] == session_id
    assert params["toolCall"]["status"] == "pending"
    assert params["toolCall"]["toolCallId"].startswith("t1-")
    assert [o["optionId"] for o in params["options"]] == ["allow_once", "allow_always", "reject_once"]
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "hello"

    call = next(u for u in updates(messages) if u["sessionUpdate"] == "tool_call")
    assert call["status"] == "pending" and call["kind"] == "edit"
    assert call["title"].startswith("write_file(")
    kinds = [(u["sessionUpdate"], u.get("status")) for u in updates(messages)]
    assert kinds == [
        ("tool_call", "pending"),
        ("tool_call_update", "in_progress"),
        ("tool_call_update", "completed"),
        ("agent_message_chunk", None),
        ("usage_update", None),
    ]
    completed = [u for u in updates(messages) if u.get("status") == "completed"][0]
    assert completed["toolCallId"] == call["toolCallId"]
    text = "".join(u["content"]["text"] for u in updates(messages) if u["sessionUpdate"] == "agent_message_chunk")
    assert text == "saved"
    assert all(u.get("messageId") for u in updates(messages) if u["sessionUpdate"] == "agent_message_chunk")
    usage = next(u for u in updates(messages) if u["sessionUpdate"] == "usage_update")
    assert usage == {"sessionUpdate": "usage_update", "used": 40, "size": 8192}

    response = messages[-1]
    assert response["id"] == request_id
    assert response["result"] == {"stopReason": "end_turn",
                                  "_meta": {"usage": {"promptTokens": 40, "completionTokens": 7}}}
    assert roles(w.server._sessions[session_id].messages) == ["system", "user", "assistant", "tool", "assistant"]
    w.close()


def test_a_rejected_write_is_not_run_and_the_turn_still_ends(workspace):
    w = Wire(workspace, [
        tool_reply(("write_file", {"path": "out.txt", "content": "hello"})),
        text_reply("not saved"),
    ])
    initialize(w)
    session_id = new_session(w, workspace)
    _, messages = prompt(w, session_id, "write", answer=lambda r: "reject_once")

    assert not (workspace / "out.txt").exists()
    statuses = [(u["sessionUpdate"], u.get("status")) for u in updates(messages)]
    assert statuses == [("tool_call", "pending"), ("tool_call_update", "failed"), ("agent_message_chunk", None)]
    assert messages[-1]["result"]["stopReason"] == "end_turn"
    w.close()


def test_allow_always_is_remembered_for_the_session_only(workspace):
    w = Wire(workspace, [
        tool_reply(("write_file", {"path": "a.txt", "content": "a"})), text_reply("a"),
        tool_reply(("write_file", {"path": "b.txt", "content": "b"})), text_reply("b"),
        tool_reply(("write_file", {"path": "c.txt", "content": "c"})), text_reply("c"),
    ])
    initialize(w)
    first = new_session(w, workspace)
    _, messages = prompt(w, first, "write a", answer=lambda r: "allow_always")
    assert messages[-1]["result"]["stopReason"] == "end_turn"

    def never_asked(request: dict) -> str:
        pytest.fail("the same tool in the same workspace was asked again in the same session")

    prompt(w, first, "write b", answer=never_asked)
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "b"

    asked: list[dict] = []
    second = new_session(w, workspace)
    prompt(w, second, "write c", answer=lambda r: asked.append(r) or "allow_once")
    assert len(asked) == 1  # a new session starts empty
    w.close()


def test_cancel_while_waiting_for_approval_ends_the_turn_as_cancelled(workspace):
    w = Wire(workspace, [tool_reply(("write_file", {"path": "out.txt", "content": "x"})), text_reply("unused")])
    initialize(w)
    session_id = new_session(w, workspace)

    def cancel_instead_of_answering(request: dict) -> str | None:
        w.notify("session/cancel", {"sessionId": session_id})
        return None  # the client answers the pending permission request with "cancelled"

    _, messages = prompt(w, session_id, "write", answer=cancel_instead_of_answering)

    assert messages[-1]["result"]["stopReason"] == "cancelled"
    assert not (workspace / "out.txt").exists()
    history = w.server._sessions[session_id].messages
    assert roles(history) == ["system", "user", "assistant", "tool"]  # the call got its answer
    assert_tool_calls_answered(history)
    w.close()


def test_cancel_during_a_model_reply_drops_the_partial_reply(workspace):
    w = Wire(workspace, [])
    initialize(w)
    session_id = new_session(w, workspace)
    w.client.responses.append(Stream([chunk(content="he"), Run(lambda: cancel_now(w, session_id)),
                                      chunk(content="llo")]))

    _, messages = prompt(w, session_id, "hi")

    assert messages[-1]["result"]["stopReason"] == "cancelled"
    deltas = [u["content"]["text"] for u in updates(messages) if u["sessionUpdate"] == "agent_message_chunk"]
    assert deltas == ["he"]
    # No reply was recorded, so the user message is dropped (the T04 rule, now in the core).
    assert roles(w.server._sessions[session_id].messages) == ["system"]
    w.close()


def test_a_second_prompt_while_one_runs_is_a_json_rpc_error(workspace):
    release = threading.Event()
    w = Wire(workspace, [Stream([chunk(content="wait"), Run(lambda: release.wait(TIMEOUT)), chunk(content="!")])])
    initialize(w)
    session_id = new_session(w, workspace)
    first = w.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "one"}]})
    second = w.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "two"}]})

    rejected = w.read_until(second)[-1]
    assert rejected["error"]["code"] == -32600
    assert "already running" in rejected["error"]["message"]

    release.set()
    assert w.read_until(first)[-1]["result"]["stopReason"] == "end_turn"
    w.close()


def test_eof_cancels_a_running_turn_and_shuts_down(workspace):
    w = Wire(workspace, [])
    initialize(w)
    session_id = new_session(w, workspace)
    w.client.responses.append(Stream([chunk(content="x"),
                                      Run(lambda: wait_for(lambda: w.server._sessions[session_id].cancel.is_set())),
                                      chunk(content="y")]))
    request_id = w.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]})

    w.inbox.close()  # EOF while the turn runs
    response = w.read_until(request_id)[-1]

    assert response["result"]["stopReason"] == "cancelled"
    w.thread.join(TIMEOUT)
    assert not w.thread.is_alive()


def test_ollama_down_is_a_json_rpc_error_and_the_server_goes_on(workspace):
    settings = Settings(workspace=workspace, host="http://127.0.0.1:1")
    w = Wire(workspace, [Stream([Raise(httpx.ConnectError("refused"))]), text_reply("back")], settings=settings)
    initialize(w)
    session_id = new_session(w, workspace)

    _, messages = prompt(w, session_id, "hi")

    error = messages[-1]["error"]
    assert error["code"] == -32603
    assert error["message"].startswith("Cannot reach Ollama at http://127.0.0.1:1")
    assert "ollama serve" in error["message"]
    assert roles(w.server._sessions[session_id].messages) == ["system"]  # no reply: the user message is dropped

    _, messages = prompt(w, session_id, "again")
    assert messages[-1]["result"]["stopReason"] == "end_turn"
    w.close()


# ----- framing, errors and the rest of the surface -------------------------------------------------


def test_malformed_lines_unknown_methods_and_bad_params_get_json_rpc_errors(workspace):
    w = Wire(workspace, [])
    w.send("{not json\n")
    parse = w.read()
    assert parse["error"]["code"] == -32700 and parse["id"] is None

    w.send("[1, 2]\n")
    assert w.read()["error"]["code"] == -32600

    assert w.read_until(w.request("session/new", {"cwd": str(workspace), "mcpServers": []}))[-1]["error"]["code"] == -32600

    init_bad = w.read_until(w.request("initialize", {}))[-1]
    assert init_bad["error"]["code"] == -32602

    initialize(w)
    assert w.read_until(w.request("nope", {}))[-1]["error"]["code"] == -32601
    assert w.read_until(w.request("session/new", {"cwd": "relative", "mcpServers": []}))[-1]["error"]["code"] == -32602
    assert w.read_until(w.request("session/new", {"cwd": str(workspace)}))[-1]["error"]["code"] == -32602
    assert w.read_until(w.request("session/new", ["not", "an", "object"]))[-1]["error"]["code"] == -32602

    missing = w.read_until(w.request("session/prompt", {"sessionId": "sess-none", "prompt": []}))[-1]
    assert missing["error"]["code"] == -32002

    session_id = new_session(w, workspace)
    image = w.read_until(w.request("session/prompt", {"sessionId": session_id, "prompt": [
        {"type": "image", "data": "", "mimeType": "image/png"}]}))[-1]
    assert image["error"]["code"] == -32602 and "image" in image["error"]["message"]
    empty = w.read_until(w.request("session/prompt", {"sessionId": session_id, "prompt": []}))[-1]
    assert empty["error"]["code"] == -32602

    # A notification with an unknown method gets no answer; the next request is still answered.
    w.send(json.dumps({"jsonrpc": "2.0", "method": "session/unknown", "params": {}}) + "\n")
    w.client.responses.append(text_reply("ok"))
    _, messages = prompt(w, session_id, "still here")
    assert messages[-1]["result"]["stopReason"] == "end_turn"
    w.close()


def test_cancel_for_an_unknown_session_is_ignored(workspace):
    w = Wire(workspace, [])
    initialize(w)
    w.notify("session/cancel", {"sessionId": "sess-unknown"})
    assert w.read_until(w.request("initialize", {"protocolVersion": 1}))[-1]["result"]["protocolVersion"] == 1
    w.close()


def test_session_new_ignores_the_mcp_servers_it_is_given_and_says_so(workspace):
    w = Wire(workspace, [])
    initialize(w)
    request_id = w.request("session/new", {"cwd": str(workspace), "mcpServers": [
        {"name": "files", "command": "node", "args": [], "env": []}]})
    response = w.read_until(request_id)[-1]
    assert response["result"]["sessionId"].startswith("sess-")
    assert any("MCP" in line for line in w.log)
    w.close()


def test_stdout_carries_only_protocol_lines_and_warnings_go_to_stderr(workspace):
    """serve_stdio end to end over injected streams: a stray print from inside a turn lands on stderr."""
    done = threading.Event()
    stdout = io.StringIO()
    stderr = io.StringIO()

    class Writer:
        def write(self, text: str) -> None:
            stdout.write(text)
            if json.loads(text).get("id") == 3:  # the prompt's response: the client is finished
                done.set()

        def flush(self) -> None:
            pass

    client = FakeOllama([Stream([chunk(content="a"), Run(lambda: print("stray output from inside a turn")),
                                 chunk(content="b")])])

    def reader():
        """The client: initialize, session/new, then a prompt to the session id the server returned."""
        yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}) + "\n"
        yield json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                          "params": {"cwd": str(workspace), "mcpServers": []}}) + "\n"
        session_id = None
        while session_id is None:  # the session id is random, so it is read back from stdout
            time.sleep(0.01)
            for line in stdout.getvalue().splitlines():
                message = json.loads(line)
                if message.get("id") == 2 and "result" in message:
                    session_id = message["result"]["sessionId"]
        yield json.dumps({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
            "sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]}}) + "\n"
        done.wait(TIMEOUT)

    serve_stdio(Settings(workspace=workspace), client=client, reader=reader(), writer=Writer(), errors=stderr)

    out_lines = [line for line in stdout.getvalue().splitlines() if line]
    assert out_lines, "the server wrote nothing"
    for line in out_lines:
        message = json.loads(line)  # every stdout line is a JSON-RPC message
        assert message["jsonrpc"] == "2.0"
    assert "stray output" not in stdout.getvalue()
    assert "stray output from inside a turn" in stderr.getvalue()


# ----- shutdown, the model check and the input limits (review round) ------------------------------


def test_shutdown_waits_one_shared_deadline_for_all_hung_turns(workspace, monkeypatch):
    """Two turns that ignore their cancel: shutdown takes about one deadline, not one per turn."""
    monkeypatch.setattr(server_module, "SHUTDOWN_DEADLINE_S", 1.0)
    hang = threading.Event()
    entered = threading.Semaphore(0)

    def stuck():
        entered.release()
        hang.wait(TIMEOUT)  # a turn stuck where the cancel is not checked

    w = Wire(workspace, [Stream([chunk(content="a"), Run(stuck)]), Stream([chunk(content="b"), Run(stuck)])])
    try:
        initialize(w)
        for _ in range(2):  # two sessions, each with a prompt that goes on running
            w.request("session/prompt", {"sessionId": new_session(w, workspace),
                                         "prompt": [{"type": "text", "text": "hi"}]})
        assert entered.acquire(timeout=TIMEOUT) and entered.acquire(timeout=TIMEOUT)  # both turns are stuck
        started = time.monotonic()
        w.inbox.close()
        w.thread.join(TIMEOUT)
        elapsed = time.monotonic() - started
        assert not w.thread.is_alive()
        assert 0.9 <= elapsed < 1.8, f"two hung turns took {elapsed:.2f} s; one shared 1 s deadline expected"
        assert any("did not stop within 1 s" in line for line in w.log)
    finally:
        hang.set()


def test_finished_turns_are_pruned_from_the_worker_list(workspace):
    w = Wire(workspace, [text_reply("one"), text_reply("two"), text_reply("three")])
    initialize(w)
    session_id = new_session(w, workspace)
    for word in ("a", "b", "c"):
        prompt(w, session_id, word)
    assert len(w.server._workers) <= 2  # three turns ran; the finished ones are not kept
    w.close()


def test_a_session_beyond_the_cap_gets_invalid_request(workspace, monkeypatch):
    monkeypatch.setattr(server_module, "MAX_SESSIONS", 2)
    w = Wire(workspace, [])
    initialize(w)
    new_session(w, workspace)
    new_session(w, workspace)
    response = w.read_until(w.request("session/new", {"cwd": str(workspace), "mcpServers": []}))[-1]
    assert response["error"]["code"] == -32600
    assert "Too many sessions" in response["error"]["message"]
    w.close()


def test_a_model_check_that_blocks_does_not_delay_initialize(workspace):
    """The startup model check runs beside the server: a black-holed Ollama cannot hold up initialize."""
    release = threading.Event()
    answered = threading.Event()
    seen_while_blocked: list[bool] = []
    stderr = io.StringIO()

    class BlackHole:
        def show(self, model):
            release.wait(TIMEOUT)
            raise ConnectionError("no answer from the host")

    class Writer:
        def write(self, text: str) -> None:
            message = json.loads(text)
            if message.get("id") == 1:
                seen_while_blocked.append(not release.is_set())
                answered.set()

        def flush(self) -> None:
            pass

    def reader():
        yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}) + "\n"
        answered.wait(TIMEOUT)  # the client waits for the answer before it ends stdin

    try:
        serve_stdio(Settings(workspace=workspace), client=BlackHole(), reader=reader(), writer=Writer(),
                    errors=stderr)
    finally:
        release.set()
    assert answered.is_set(), "initialize was not answered while the model check was blocked"
    assert seen_while_blocked == [True]
    wait_for(lambda: "Warning:" in stderr.getvalue())  # the check's own warning still reaches stderr


def test_a_response_with_a_non_string_id_is_logged_and_ignored(workspace):
    w = Wire(workspace, [])
    w.send(json.dumps({"jsonrpc": "2.0", "id": [1], "result": {}}) + "\n")
    w.send(json.dumps({"jsonrpc": "2.0", "id": {"a": 1}, "error": {"code": 1, "message": "x"}}) + "\n")
    initialize(w)  # the server is still answering
    assert sum("not a string" in line for line in w.log) == 2
    w.close()


def test_deeply_nested_json_is_a_parse_error_and_the_server_goes_on(workspace):
    w = Wire(workspace, [])
    w.send("[" * 200_000 + "]" * 200_000 + "\n")
    error = w.read()
    assert error["error"]["code"] == -32700
    assert error["id"] is None
    initialize(w)
    w.close()


def test_a_prompt_with_a_lone_surrogate_is_invalid_params_and_history_is_untouched(workspace):
    w = Wire(workspace, [])
    initialize(w)
    session_id = new_session(w, workspace)
    request_id = w.request("session/prompt", {"sessionId": session_id,
                                              "prompt": [{"type": "text", "text": "bad \ud800 text"}]})
    response = w.read()
    assert response["id"] == request_id
    assert response["error"]["code"] == -32602
    session = w.server._sessions[session_id]
    assert len(session.messages) == 1  # only the system message: the user message was never added
    assert not session.busy
    w.close()


def test_a_line_over_the_cap_is_refused_with_invalid_request(workspace, monkeypatch):
    monkeypatch.setattr(server_module, "MAX_LINE_CHARS", 200)
    w = Wire(workspace, [])
    w.send("x" * 300 + "\n")
    error = w.read()
    assert error["error"]["code"] == -32600
    assert error["id"] is None
    initialize(w)
    w.close()


def test_the_bounded_reader_discards_a_long_line_and_keeps_the_next_one():
    stream = io.StringIO("ab\n" + "c" * 50 + "\n" + "d\n" + "e" * 50)
    assert list(_bounded_lines(stream, 10)) == ["ab\n", OVERSIZE_LINE, "d\n", OVERSIZE_LINE]
