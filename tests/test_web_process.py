"""The real web server (T15): `python -m wildcat_code_agent serve --web` as a subprocess, with uvicorn and a real
WebSocket client, against an unreachable Ollama.

Proves the start-up line carries the URL and token, a missing, wrong or foreign-origin client is refused
at the handshake, a client with the token gets JSON-RPC answers, and nothing but log lines goes to stderr.
The process is ours: it is stopped in `finally`, by its own handle.
"""

import json
import re
import subprocess
import sys
import threading

import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

TIMEOUT_S = 60
BANNER = re.compile(r"WebSocket: ws://127\.0\.0\.1:(\d+)/ws\?token=(\S+)")


def _start(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")  # the user's own config is never read
    proc = subprocess.Popen(
        [sys.executable, "-m", "wildcat_code_agent", "serve", "--web", "--port", "0", "--no-plugins",
         "--host", "http://127.0.0.1:1", "--config", str(config), "--cwd", str(tmp_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lines: list[str] = []
    found = threading.Event()
    found_match: dict = {}

    def read_stderr() -> None:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            lines.append(line)
            match = BANNER.search(line)
            if match and not found.is_set():
                found_match["port"], found_match["token"] = int(match.group(1)), match.group(2)
                found.set()

    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    watchdog = threading.Timer(TIMEOUT_S, proc.kill)
    watchdog.start()
    if not found.wait(TIMEOUT_S):
        proc.kill()
        raise AssertionError("the server did not print its WebSocket URL: " + "\n".join(lines))
    return proc, found_match["port"], found_match["token"], lines, watchdog, reader


def test_the_web_server_refuses_bad_clients_and_serves_a_real_client(tmp_path):
    proc, port, token, lines, watchdog, reader = _start(tmp_path)
    try:
        url = f"ws://127.0.0.1:{port}/ws"
        own_origin = f"http://127.0.0.1:{port}"

        with pytest.raises(InvalidStatus) as missing:
            connect(url, open_timeout=TIMEOUT_S)
        assert missing.value.response.status_code == 403

        with pytest.raises(InvalidStatus) as wrong:
            connect(f"{url}?token=wrong", open_timeout=TIMEOUT_S)
        assert wrong.value.response.status_code == 403

        with pytest.raises(InvalidStatus) as foreign:
            connect(f"{url}?token={token}", origin="http://evil.example", open_timeout=TIMEOUT_S)
        assert foreign.value.response.status_code == 403

        with connect(f"{url}?token={token}", origin=own_origin, open_timeout=TIMEOUT_S) as ws:
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": 1}}))
            answer = json.loads(ws.recv(timeout=TIMEOUT_S))
            assert answer["id"] == 1 and answer["result"]["protocolVersion"] == 1
    finally:
        watchdog.cancel()
        proc.kill()  # our own process: stop it now that the checks are done
        proc.wait(timeout=TIMEOUT_S)
        reader.join(TIMEOUT_S)
        proc.stdout.close()
        proc.stderr.close()

    # The token is printed only in the two URLs: the page link (its fragment) and the WebSocket URL.
    assert not any(token in line for line in lines if "WebSocket:" not in line and "#token=" not in line)


def test_a_line_over_the_character_limit_gets_32600_and_the_socket_stays_open(tmp_path):
    from wildcat_code_agent.frontends.server import MAX_LINE_CHARS

    proc, port, token, lines, watchdog, reader = _start(tmp_path)
    try:
        with connect(f"ws://127.0.0.1:{port}/ws?token={token}", origin=f"http://127.0.0.1:{port}",
                     open_timeout=TIMEOUT_S, max_size=None) as ws:
            # One frame of MAX_LINE_CHARS + 1 ASCII characters: the transport accepts it, the server refuses the line.
            ws.send("x" * (MAX_LINE_CHARS + 1))
            answer = json.loads(ws.recv(timeout=TIMEOUT_S))
            assert answer["error"]["code"] == -32600 and answer["id"] is None
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": 1}}))
            assert json.loads(ws.recv(timeout=TIMEOUT_S))["id"] == 2
    finally:
        watchdog.cancel()
        proc.kill()
        proc.wait(timeout=TIMEOUT_S)
        reader.join(TIMEOUT_S)
        proc.stdout.close()
        proc.stderr.close()
