"""The WebSocket transport (T15), in-process: the token and Origin checks, and a turn driven over the socket.

The app runs under Starlette's TestClient with the fake Ollama client from tests/fakes.py. A refused
connection is one the server closes before accepting it, so the client sees WebSocketDisconnect with code 1008.
"""

import json
import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fakes import FakeOllama, text_reply, tool_reply
from local_agent.config import Settings
from local_agent.frontends import web

PORT = 8765
TIMEOUT_S = 5
TOKEN = "test-token-abc_123"
GOOD_ORIGINS = [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"]


def _app(workspace: Path, responses: list | None = None):
    client = FakeOllama(responses or [])
    settings = Settings(workspace=workspace)
    return web.create_app(settings, token=TOKEN, port=PORT, client=client, log=lambda line: None), client


@pytest.fixture
def app_client(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        yield client


def _refused(client: TestClient, url: str, headers: dict | None = None) -> int:
    """Open a WebSocket that the server must refuse; return the close code."""
    with pytest.raises(WebSocketDisconnect) as refusal:
        with client.websocket_connect(url, headers=headers or {}):
            pass
    return refusal.value.code


# ----- the checks themselves -------------------------------------------------------------------------

def test_the_token_must_match_exactly():
    assert web.token_ok(TOKEN, TOKEN)
    assert not web.token_ok(None, TOKEN)
    assert not web.token_ok("", TOKEN)
    assert not web.token_ok(TOKEN + "x", TOKEN)
    assert not web.token_ok(TOKEN[:-1], TOKEN)
    assert not web.token_ok("\ud800" * 3, TOKEN)  # a lone surrogate is a wrong token, not a crash


def test_origin_policy():
    assert web.origin_ok(None, PORT)                       # no Origin: a non-browser client
    for origin in GOOD_ORIGINS:
        assert web.origin_ok(origin, PORT)
    for origin in ["https://127.0.0.1:%d" % PORT, f"http://127.0.0.1:{PORT + 1}", "http://evil.example",
                   f"http://localhost.evil.example:{PORT}", "null", f"http://[::1]:{PORT}"]:
        assert not web.origin_ok(origin, PORT), origin


def test_new_token_is_random_and_url_safe():
    tokens = {web.new_token() for _ in range(5)}
    assert len(tokens) == 5
    for token in tokens:
        assert len(token) >= 40
        assert all(c.isalnum() or c in "-_" for c in token)


# ----- the WebSocket handshake -----------------------------------------------------------------------

def test_a_missing_token_is_refused_before_accept(app_client):
    assert _refused(app_client, "/ws") == web.WS_POLICY_VIOLATION


def test_a_wrong_token_is_refused(app_client):
    assert _refused(app_client, "/ws?token=not-the-token") == web.WS_POLICY_VIOLATION
    assert _refused(app_client, "/ws?token=") == web.WS_POLICY_VIOLATION


def test_a_wrong_bearer_token_or_another_scheme_is_refused(app_client):
    assert _refused(app_client, "/ws", {"Authorization": "Bearer wrong"}) == web.WS_POLICY_VIOLATION
    assert _refused(app_client, "/ws", {"Authorization": f"Basic {TOKEN}"}) == web.WS_POLICY_VIOLATION
    assert _refused(app_client, f"/ws?token={TOKEN}", {"Authorization": "Bearer wrong"}) == web.WS_POLICY_VIOLATION


@pytest.mark.parametrize("origin", ["https://127.0.0.1:%d" % PORT, f"http://127.0.0.1:{PORT + 1}",
                                    "http://evil.example", f"http://evil.example:{PORT}"])
def test_a_foreign_origin_is_refused_even_with_the_right_token(app_client, origin):
    assert _refused(app_client, f"/ws?token={TOKEN}", {"Origin": origin}) == web.WS_POLICY_VIOLATION


def test_the_query_token_and_the_bearer_header_both_open_the_socket(app_client):
    with app_client.websocket_connect(f"/ws?token={TOKEN}", headers={"Origin": GOOD_ORIGINS[0]}) as ws:
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}))
        assert json.loads(ws.receive_text())["result"]["protocolVersion"] == 1
    with app_client.websocket_connect("/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}))
        assert json.loads(ws.receive_text())["id"] == 1


@pytest.mark.parametrize("origin", GOOD_ORIGINS)
def test_the_servers_own_origins_are_accepted(app_client, origin):
    with app_client.websocket_connect(f"/ws?token={TOKEN}", headers={"Origin": origin}) as ws:
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {"protocolVersion": 1}}))
        assert json.loads(ws.receive_text())["id"] == 9


def test_the_page_does_not_open_the_agent_and_has_no_docs_routes(app_client):
    response = app_client.get("/")
    assert response.status_code == 200
    assert "<title>local-agent</title>" in response.text and TOKEN not in response.text
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert app_client.get(path).status_code == 404


# ----- a turn over the WebSocket ---------------------------------------------------------------------

class _Socket:
    """The client end of one WebSocket: sends JSON-RPC messages and reads the frames back."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._ids = 0

    def request(self, method: str, params: dict) -> int:
        self._ids += 1
        self._ws.send_text(json.dumps({"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params}))
        return self._ids

    def read(self) -> dict:
        return json.loads(self._ws.receive_text())

    def read_until(self, request_id: int, *, answer: str = "allow_once") -> list[dict]:
        messages = []
        while True:
            message = self.read()
            messages.append(message)
            if message.get("method") == "session/request_permission":
                self._ws.send_text(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                    "outcome": {"outcome": "selected", "optionId": answer}}}))
            elif message.get("id") == request_id and "method" not in message:
                return messages


def _open(app_client, *, origin: str = GOOD_ORIGINS[0]):
    return app_client.websocket_connect(f"/ws?token={TOKEN}", headers={"Origin": origin})


def test_a_text_turn_runs_over_the_websocket(tmp_path):
    app, client = _app(tmp_path, [text_reply("Hello from the model.", prompt_eval_count=12, eval_count=5)])
    with TestClient(app) as http:
        with http.websocket_connect(f"/ws?token={TOKEN}", headers={"Origin": GOOD_ORIGINS[1]}) as ws:
            s = _Socket(ws)
            s.read_until(s.request("initialize", {"protocolVersion": 1}))
            session_id = s.read_until(s.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))[-1][
                "result"]["sessionId"]
            messages = s.read_until(s.request("session/prompt", {
                "sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]}))
    updates = [m["params"]["update"] for m in messages if m.get("method") == "session/update"]
    text = "".join(u["content"]["text"] for u in updates if u["sessionUpdate"] == "agent_message_chunk")
    assert text == "Hello from the model."
    assert {u["messageId"] for u in updates if u["sessionUpdate"] == "agent_message_chunk"}
    assert any(u["sessionUpdate"] == "usage_update" for u in updates)
    final = messages[-1]
    assert final["result"]["stopReason"] == "end_turn"
    assert final["result"]["_meta"]["usage"] == {"promptTokens": 12, "completionTokens": 5}
    assert client.calls, "the fake model was called"


def test_a_write_is_approved_over_the_websocket_and_lands_in_the_workspace(tmp_path):
    app, _ = _app(tmp_path, [tool_reply(("write_file", {"path": "out.txt", "content": "from web"})),
                             text_reply("Written.")])
    with TestClient(app) as http:
        with http.websocket_connect(f"/ws?token={TOKEN}") as ws:  # no Origin: a non-browser client
            s = _Socket(ws)
            s.read_until(s.request("initialize", {"protocolVersion": 1}))
            session_id = s.read_until(s.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))[-1][
                "result"]["sessionId"]
            messages = s.read_until(s.request("session/prompt", {
                "sessionId": session_id, "prompt": [{"type": "text", "text": "write it"}]}))
    permissions = [m for m in messages if m.get("method") == "session/request_permission"]
    assert len(permissions) == 1
    assert [o["optionId"] for o in permissions[0]["params"]["options"]] == ["allow_once", "allow_always", "reject_once"]
    assert messages[-1]["result"]["stopReason"] == "end_turn"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "from web"


def test_an_edit_request_carries_the_diff_preview(tmp_path):
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    app, _ = _app(tmp_path, [tool_reply(("edit_file", {"path": "notes.txt", "old_text": "beta",
                                                        "new_text": "gamma"})),
                             text_reply("Edited.")])
    with TestClient(app) as http:
        with http.websocket_connect(f"/ws?token={TOKEN}") as ws:
            s = _Socket(ws)
            s.read_until(s.request("initialize", {"protocolVersion": 1}))
            session_id = s.read_until(s.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))[-1][
                "result"]["sessionId"]
            messages = s.read_until(s.request("session/prompt", {
                "sessionId": session_id, "prompt": [{"type": "text", "text": "edit it"}]}))
    permission = next(m for m in messages if m.get("method") == "session/request_permission")
    preview = permission["params"]["toolCall"]["preview"]
    assert preview["kind"] == "diff" and preview["path"].endswith("notes.txt")
    assert "-beta" in preview["text"] and "+gamma" in preview["text"]
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_sessions_belong_to_their_connection(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as http:
        with http.websocket_connect(f"/ws?token={TOKEN}") as first:
            a = _Socket(first)
            a.read_until(a.request("initialize", {"protocolVersion": 1}))
            session_id = a.read_until(a.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))[-1][
                "result"]["sessionId"]
            with http.websocket_connect(f"/ws?token={TOKEN}") as second:
                b = _Socket(second)
                b.read_until(b.request("initialize", {"protocolVersion": 1}))
                error = b.read_until(b.request("session/prompt", {
                    "sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]}))[-1]
                assert error["error"]["code"] == -32002


def test_a_message_before_initialize_gets_the_stdio_error(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as http:
        with http.websocket_connect(f"/ws?token={TOKEN}") as ws:
            s = _Socket(ws)
            response = s.read_until(s.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))[-1]
            assert response["error"]["code"] == -32600


def test_a_malformed_frame_is_answered_and_the_socket_stays_open(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as http:
        with http.websocket_connect(f"/ws?token={TOKEN}") as ws:
            ws.send_text("{not json")
            assert json.loads(ws.receive_text())["error"]["code"] == -32700
            s = _Socket(ws)
            assert s.read_until(s.request("initialize", {"protocolVersion": 1}))[-1]["result"]["protocolVersion"] == 1


def test_binding_is_loopback_and_port_zero_picks_a_free_port():
    sock = web.bind_loopback(0)
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1"
        assert port > 0
    finally:
        sock.close()


def test_a_disconnect_during_a_permission_wait_cancels_the_turn_and_frees_the_thread(tmp_path):
    threads: list = []
    client = FakeOllama([tool_reply(("write_file", {"path": "out.txt", "content": "must not be written"})),
                         text_reply("unused")])
    app = web.create_app(Settings(workspace=tmp_path), token=TOKEN, port=PORT, client=client,
                         log=lambda line: None, threads=threads)
    with TestClient(app) as http:
        with http.websocket_connect(f"/ws?token={TOKEN}") as ws:
            s = _Socket(ws)
            s.read_until(s.request("initialize", {"protocolVersion": 1}))
            session_id = s.read_until(s.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}))[-1][
                "result"]["sessionId"]
            s.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "write"}]})
            while s.read().get("method") != "session/request_permission":
                pass
            # leave without answering: the client is gone while the turn waits for its approval
    assert threads, "a connection thread was started"
    for thread in threads:
        thread.join(TIMEOUT_S)
        assert not thread.is_alive(), "the connection thread did not end after the disconnect"
    assert not (tmp_path / "out.txt").exists(), "the cancelled write must not run"


@pytest.mark.skipif(sys.platform != "win32", reason="SO_EXCLUSIVEADDRUSE is a Windows option")
def test_a_fixed_port_cannot_be_taken_by_another_socket():
    sock = web.bind_loopback(0)
    try:
        port = sock.getsockname()[1]
        other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # the setting that lets Windows share a port
        try:
            with pytest.raises(OSError):
                other.bind(("127.0.0.1", port))
        finally:
            other.close()
    finally:
        sock.close()
