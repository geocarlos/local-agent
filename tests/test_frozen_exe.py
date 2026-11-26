"""The built executable (T12): `wildcat-code-agent.exe --help` and a `serve --stdio` initialize round-trip.

Skipped unless an exe exists: set WILDCAT_CODE_AGENT_EXE to its path, or build it first with packaging\\build.ps1
(the default location is dist\\wildcat-code-agent\\wildcat-code-agent.exe). A normal `uv run pytest` never builds anything.
The exe runs from a temp folder, with an empty temp config and an unreachable Ollama, so it reads no
per-user files and does not touch a running Ollama.
"""

import json
import os
import queue
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXE = Path(os.environ["WILDCAT_CODE_AGENT_EXE"]) if os.environ.get("WILDCAT_CODE_AGENT_EXE") else REPO / "dist" / "wildcat-code-agent" / "wildcat-code-agent.exe"

pytestmark = pytest.mark.skipif(not EXE.is_file(),
                                reason=f"no built executable at {EXE} (build with packaging\\build.ps1 or set WILDCAT_CODE_AGENT_EXE)")

TIMEOUT_S = 120


def _common_args(tmp_path: Path) -> list[str]:
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    return ["--config", str(config), "--host", "http://127.0.0.1:1", "--no-plugins"]


def test_help_runs_from_a_folder_outside_the_repo(tmp_path):
    result = subprocess.run([str(EXE), "--help"], cwd=tmp_path, capture_output=True, text=True,
                            encoding="utf-8", timeout=TIMEOUT_S)
    assert result.returncode == 0, result.stderr
    assert "usage: wildcat-code-agent" in result.stdout
    assert "serve --stdio" in result.stdout


def test_serve_stdio_initialize_round_trip_writes_only_json_to_stdout(tmp_path):
    proc = subprocess.Popen([str(EXE), "serve", "--stdio", "--cwd", str(tmp_path), *_common_args(tmp_path)],
                            cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    lines: queue.Queue[bytes] = queue.Queue()

    def pump() -> None:
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(b"")  # end of stdout

    threading.Thread(target=pump, daemon=True).start()
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": 1, "clientCapabilities": {}}}
        proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        proc.stdin.flush()
        raw = lines.get(timeout=TIMEOUT_S)
        assert raw, "the executable closed stdout before answering initialize"
        message = json.loads(raw.decode("utf-8"))  # every stdout line must be a JSON object
        assert message["id"] == 1
        assert message["result"]["protocolVersion"] == 1
        assert message["result"]["agentInfo"]["name"] == "wildcat-code-agent"
        proc.stdin.close()  # EOF ends the server
        assert proc.wait(timeout=TIMEOUT_S) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    rest = []
    while True:
        raw = lines.get(timeout=TIMEOUT_S)
        if not raw:
            break
        rest.append(raw)
    for raw in rest:
        json.loads(raw.decode("utf-8"))
