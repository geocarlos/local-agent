"""A fake Ollama HTTP server with a scripted model, for tests that run `local-agent serve --web` as a process.

It answers the two routes the agent uses: `POST /api/show` (the capability probe at start-up) and `POST /api/chat`
(streamed, one JSON object per line, as Ollama does). The script depends on the last message:

- last message is a tool result: a reply that says the edit is done, streamed in pieces;
- last user message contains "EDIT": a call to edit_file that replaces "world" with "there" in hello.txt
  (relative paths resolve in the session's workspace);
- anything else: a plain reply, streamed in pieces.

The server runs on 127.0.0.1 in a thread. `start()` returns (server, base_url); call `server.shutdown()` to stop it.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAPABILITIES = ["completion", "tools"]
PLAIN_REPLY = ["Hello ", "from the ", "scripted model."]
DONE_REPLY = ["The edit is done: ", "hello.txt now says ", "Hello there."]


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def _text_chunk(piece: str) -> dict:
    return {"model": "scripted", "created_at": "2026-01-01T00:00:00Z",
            "message": {"role": "assistant", "content": piece}, "done": False}


def script_for(messages: list[dict]) -> list[dict]:
    """The chunks of one reply, as Ollama streams them (the last chunk is the done chunk with token counts)."""
    last = messages[-1] if messages else {}
    user_text = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    if last.get("role") == "tool":
        pieces = DONE_REPLY
        chunks = [_text_chunk(piece) for piece in pieces]
    elif "EDIT" in user_text:
        chunks = [{"model": "scripted", "created_at": "2026-01-01T00:00:00Z",
                   "message": {"role": "assistant", "content": "", "tool_calls": [
                       {"function": {"name": "edit_file", "arguments": {
                           "path": "hello.txt", "old_text": "world", "new_text": "there"}}}]},
                   "done": False}]
    else:
        chunks = [_text_chunk(piece) for piece in PLAIN_REPLY]
    chunks.append({"model": "scripted", "created_at": "2026-01-01T00:00:00Z",
                   "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                   "prompt_eval_count": 42, "eval_count": 12})
    return chunks


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 (the base class's signature)
        pass  # the test's output stays clean

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):  # noqa: N802 (the base class's name)
        if self.path == "/api/show":
            self._body()
            self._json({"capabilities": CAPABILITIES, "model_info": {}})
        elif self.path == "/api/chat":
            body = self._body()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in script_for(body.get("messages", [])):
                self.wfile.write(_line(chunk))
                self.wfile.flush()
        else:
            self.send_error(404)

    def _json(self, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start() -> tuple[ThreadingHTTPServer, str]:
    """Start the fake server on a free loopback port. Returns the server and its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"
