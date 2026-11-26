"""The desktop window command (T18), with the window replaced by a fake opener. The server is real: `gui.run`
starts `web.WebServer` on 127.0.0.1 with a fake Ollama client, and the fake opener plays the window's part
(it reads the page's link, asks `/info` and opens the WebSocket, as the page does, then returns: the window
was closed).

The real pywebview window is tested in tests/test_gui_window.py.
"""

import contextlib
import io
import socket
import sys
import types
import urllib.error
import urllib.parse
import urllib.request

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from fakes import FakeOllama
from wildcat_code_agent.config import Settings
from wildcat_code_agent.frontends import gui, web


def _settings(tmp_path):
    return Settings(workspace=tmp_path)


def _port_is_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _get(url: str, token: str | None) -> int:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _run(tmp_path, opener):
    errors = io.StringIO()
    code = gui.run(_settings(tmp_path), config_file=tmp_path / "missing-config.toml", client=FakeOllama([]),
                   store=None, errors=errors, open_window=opener)
    return code, errors.getvalue()


def test_the_window_opens_the_page_with_the_token_in_the_fragment(tmp_path):
    seen = {}

    def window(url):
        seen["url"] = url
        parts = urllib.parse.urlsplit(url)
        seen["fragment"] = urllib.parse.parse_qs(parts.fragment)
        seen["page"] = _get(f"http://127.0.0.1:{parts.port}/", None)
        seen["info"] = _get(f"http://127.0.0.1:{parts.port}/info", seen["fragment"]["token"][0])

    code, _ = _run(tmp_path, window)

    assert code == 0
    parts = urllib.parse.urlsplit(seen["url"])
    assert parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.path == "/"
    assert list(seen["fragment"]) == ["token"]
    assert seen["page"] == 200
    assert seen["info"] == 200  # the fragment token is the server's token


def test_the_page_websocket_accepts_the_windows_origin(tmp_path):
    seen = {}

    def window(url):
        parts = urllib.parse.urlsplit(url)
        token = urllib.parse.parse_qs(parts.fragment)["token"][0]
        origin = f"http://127.0.0.1:{parts.port}"
        with connect(f"ws://127.0.0.1:{parts.port}/ws?token={token}", origin=origin) as ws:
            seen["open"] = ws.protocol.state.name

    code, _ = _run(tmp_path, window)

    assert code == 0
    assert seen["open"] == "OPEN"


def test_closing_the_window_stops_the_server_and_its_connections(tmp_path):
    seen = {}

    def window(url):
        parts = urllib.parse.urlsplit(url)
        seen["port"] = parts.port
        token = urllib.parse.parse_qs(parts.fragment)["token"][0]
        seen["ws"] = connect(f"ws://127.0.0.1:{parts.port}/ws?token={token}", origin=f"http://127.0.0.1:{parts.port}",
                             legacy=True)

    code, errors = _run(tmp_path, window)

    assert code == 0
    assert _port_is_closed(seen["port"])
    with pytest.raises(ConnectionClosed):  # the connection was ended by the shutdown
        seen["ws"].recv(timeout=5)
    assert "did not stop" not in errors


def test_the_token_is_not_printed(tmp_path):
    seen = {}

    def window(url):
        seen["token"] = urllib.parse.parse_qs(urllib.parse.urlsplit(url).fragment)["token"][0]

    code, errors = _run(tmp_path, window)

    assert code == 0
    assert seen["token"] not in errors
    assert "Opening the wildcat-code-agent window on http://127.0.0.1:" in errors


def test_ctrl_c_in_the_window_closes_it_cleanly(tmp_path):
    seen = {}

    def window(url):
        seen["port"] = urllib.parse.urlsplit(url).port
        raise KeyboardInterrupt

    code, _ = _run(tmp_path, window)

    assert code == 0
    assert _port_is_closed(seen["port"])


def test_a_window_that_cannot_open_is_a_clear_error_with_exit_1(tmp_path):
    seen = {}

    def window(url):
        seen["port"] = urllib.parse.urlsplit(url).port
        raise gui.WindowError("the window could not open: no WebView2")

    code, errors = _run(tmp_path, window)

    assert code == 1
    assert "Error: the window could not open: no WebView2" in errors
    assert _port_is_closed(seen["port"])  # the server is stopped even when the window failed


def test_a_server_that_does_not_start_is_an_error_and_no_window_opens(tmp_path, monkeypatch):
    def fail(self, timeout_s=0):
        raise web.ServerStartError("the plugins or MCP servers did not start: boom")

    monkeypatch.setattr(web.WebServer, "start", fail)
    opened = []

    code, errors = _run(tmp_path, opened.append)

    assert code == 1
    assert opened == []
    assert "Error: the plugins or MCP servers did not start: boom" in errors


def test_ctrl_c_during_start_up_stops_the_server_and_opens_no_window(tmp_path, monkeypatch):
    stopped = []
    real_stop = web.WebServer.stop

    def interrupted(self, timeout_s=0):
        raise KeyboardInterrupt

    def record_stop(self):
        stopped.append(True)
        real_stop(self)

    monkeypatch.setattr(web.WebServer, "start", interrupted)
    monkeypatch.setattr(web.WebServer, "stop", record_stop)
    opened = []

    code, _ = _run(tmp_path, opened.append)

    assert code == 0
    assert opened == []
    assert stopped == [True]  # the plugins and MCP servers are closed by stop


def test_show_window_without_the_backend_says_so(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)  # an import of webview now fails

    with pytest.raises(gui.WindowError, match="pywebview"):
        gui.show_window("http://127.0.0.1:1/#token=x")


def test_show_window_turns_a_failed_start_into_a_window_error(monkeypatch):
    fake = types.ModuleType("webview")

    def create_window(*args, **kwargs):
        return object()

    def start(**kwargs):
        raise RuntimeError("WebView2 runtime not found")

    fake.create_window = create_window
    fake.start = start
    monkeypatch.setitem(sys.modules, "webview", fake)

    with pytest.raises(gui.WindowError, match="WebView2 runtime not found") as error:
        gui.show_window("http://127.0.0.1:1/#token=x")
    if sys.platform == "win32":
        assert "Microsoft Edge WebView2 runtime" in str(error.value)


def test_show_window_passes_the_url_and_the_window_size(monkeypatch):
    fake = types.ModuleType("webview")
    calls = {}

    def create_window(title, url, **kwargs):
        calls["window"] = (title, url, kwargs)
        return "window"

    def start(**kwargs):
        calls["start"] = kwargs

    fake.create_window = create_window
    fake.start = start
    monkeypatch.setitem(sys.modules, "webview", fake)

    gui.show_window("http://127.0.0.1:9/#token=x")

    title, url, kwargs = calls["window"]
    assert (title, url) == ("Wildcat Code Agent", "http://127.0.0.1:9/#token=x")
    assert kwargs["width"] == gui.WINDOW_SIZE[0] and kwargs["height"] == gui.WINDOW_SIZE[1]
    assert calls["start"]["gui"] == ("edgechromium" if sys.platform == "win32" else None)


def test_ensure_streams_replaces_missing_streams_with_a_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    replaced = []
    try:
        path = gui.ensure_streams(tmp_path)
        replaced = [sys.stdout, sys.stderr]
        print("to the log", file=sys.stderr, flush=True)
        assert path == tmp_path / gui.GUI_LOG_NAME
        assert "to the log" in path.read_text(encoding="utf-8")
        assert gui.ensure_streams(tmp_path) is None  # nothing to replace the second time
    finally:
        for stream in replaced:
            with contextlib.suppress(Exception):
                stream.close()


def _open_log_streams(tmp_path, monkeypatch, *, limit: int) -> list:
    """Run ensure_streams the way the windowed exe does (both streams missing); return the streams to close."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(gui, "GUI_LOG_MAX_BYTES", limit)
    gui.ensure_streams(tmp_path)
    return [sys.stdout, sys.stderr]


def test_gui_log_appends_per_start_and_moves_a_big_one_aside(tmp_path, monkeypatch):
    log = tmp_path / gui.GUI_LOG_NAME
    log.write_text("earlier run\n", encoding="utf-8")
    streams = _open_log_streams(tmp_path, monkeypatch, limit=1000)  # small: appended to
    try:
        print("this run", file=sys.stderr, flush=True)
    finally:
        for stream in streams:
            with contextlib.suppress(Exception):
                stream.close()
    text = log.read_text(encoding="utf-8")
    assert text.startswith("earlier run\n")
    assert "--- wildcat-code-agent gui started " in text and "this run" in text
    assert not (tmp_path / (gui.GUI_LOG_NAME + ".1")).exists()

    streams = _open_log_streams(tmp_path, monkeypatch, limit=10)  # now over the limit: moved aside
    for stream in streams:
        with contextlib.suppress(Exception):
            stream.close()
    assert (tmp_path / (gui.GUI_LOG_NAME + ".1")).read_text(encoding="utf-8").startswith("earlier run\n")
    assert "earlier run" not in log.read_text(encoding="utf-8")


def test_ensure_streams_does_nothing_with_a_console(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert gui.ensure_streams(tmp_path) is None
    assert not (tmp_path / gui.GUI_LOG_NAME).exists()


def test_the_spec_builds_the_windowed_exe_beside_the_console_exe():
    from pathlib import Path

    spec = (Path(__file__).resolve().parent.parent / "packaging" / "wildcat-code-agent.spec").read_text(encoding="utf-8")
    assert 'name="wildcat-code-agent-gui"' in spec and "console=False" in spec
    assert 'name="wildcat-code-agent",' in spec and "console=True" in spec
    assert "gui_launcher.py" in spec and "MERGE(" in spec
    assert "COLLECT(" in spec and "window_exe" in spec  # one folder holds both exes
