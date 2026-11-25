"""The real binary (T11): `python -m local_agent serve --stdio` as a subprocess, against an unreachable Ollama.

Proves that the process speaks the protocol on stdout and nothing else: initialize, session/new and a
prompt get their JSON-RPC answers, the Ollama-down failure is a JSON-RPC error, and EOF ends the process
with exit code 0.
"""

import json
import subprocess
import sys
import threading

TIMEOUT_S = 60


def _send(proc: subprocess.Popen, message: dict) -> None:
    proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen, request_id: int) -> dict:
    """Read stdout lines until the response to request_id. Every line must be one JSON-RPC message."""
    while True:
        raw = proc.stdout.readline()
        assert raw, "the server closed stdout before answering"
        assert raw.endswith(b"\n") and not raw.endswith(b"\r\n"), "lines end with a bare \\n"
        message = json.loads(raw.decode("utf-8"))
        assert message["jsonrpc"] == "2.0"
        if message.get("id") == request_id and ("result" in message or "error" in message):
            return message


LEAKY_PLUGIN = '''\
"""A plugin that writes to the process's stdout in three ways that bypass sys.stdout."""

import os
import subprocess
import sys

from local_agent.plugin import Risk, tool

os.system("echo LEAK")                                   # a child shell inherits fd 1
os.write(1, b"LEAK2\\n")                                  # a direct write to fd 1
subprocess.run([sys.executable, "-c", "print('LEAK3')"])  # a child process with no capture


@tool(risk=Risk.READ)
def probe() -> str:
    """Return a fixed word."""
    return "ok"
'''


def test_child_processes_and_direct_writes_from_a_plugin_stay_off_the_protocol_stdout(tmp_path):
    plugin_dir = tmp_path / "tools"
    plugin_dir.mkdir()
    (plugin_dir / "leaky.py").write_text(LEAKY_PLUGIN, encoding="utf-8")
    config = tmp_path / "config.toml"
    # TOML literal string: no escaping needed for Windows paths
    config.write_text(f"plugin_dir = '{plugin_dir}'\n", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "local_agent", "serve", "--stdio",
         "--host", "http://127.0.0.1:1", "--config", str(config), "--cwd", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    watchdog = threading.Timer(TIMEOUT_S, proc.kill)
    watchdog.start()
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        assert _read_response(proc, 1)["result"]["protocolVersion"] == 1
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "session/new",
                     "params": {"cwd": str(tmp_path), "mcpServers": []}})
        assert "sessionId" in _read_response(proc, 2)["result"]
        proc.stdin.close()
        rest = proc.stdout.read()
        # every line on stdout is a JSON-RPC message: nothing from the plugin's writes got through
        for raw in rest.splitlines():
            assert json.loads(raw.decode("utf-8"))["jsonrpc"] == "2.0"
        assert proc.wait(timeout=TIMEOUT_S) == 0
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    for leaked in ("LEAK", "LEAK2", "LEAK3"):
        assert leaked in stderr, f"{leaked} should land on stderr"


def test_serve_stdio_binary_answers_only_json_and_reports_ollama_down(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")  # the user's own config is never read
    proc = subprocess.Popen(
        [sys.executable, "-m", "local_agent", "serve", "--stdio",
         "--host", "http://127.0.0.1:1",  # nothing listens here: Ollama is "down"
         "--config", str(config), "--no-plugins", "--cwd", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    watchdog = threading.Timer(TIMEOUT_S, proc.kill)  # a hung server must not hang the test run
    watchdog.start()
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        init = _read_response(proc, 1)
        assert init["result"]["protocolVersion"] == 1

        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "session/new",
                     "params": {"cwd": str(tmp_path), "mcpServers": []}})
        session_id = _read_response(proc, 2)["result"]["sessionId"]

        _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                     "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]}})
        failed = _read_response(proc, 3)
        assert failed["error"]["code"] == -32603
        assert failed["error"]["message"].startswith("Cannot reach Ollama at http://127.0.0.1:1")

        proc.stdin.close()  # EOF: the server shuts down
        rest = proc.stdout.read()
        assert rest == b"", "nothing but protocol lines on stdout"
        assert proc.wait(timeout=TIMEOUT_S) == 0
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    assert "Cannot reach Ollama" in stderr  # the startup check is reported on stderr, not stdout
