"""The real desktop window (T18): `gui.run` with pywebview's Edge WebView2 window, on Windows.

A window test shows a real window for a few seconds. The probe runs on its own thread: it waits until the page
reports "Connected" (the page loaded, `/info` answered, the WebSocket opened with the window's own origin), reads
the page's state, and closes the window. A watchdog closes it after 90 s if the probe hangs.

Skipped off Windows, and when the backend is missing (pywebview or pythonnet does not load, or the WebView2
runtime is not installed). With the CI environment variable set, a missing backend fails the test instead.
"""

import contextlib
import functools
import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from fakes import FakeOllama
from local_agent.config import Settings
from local_agent.frontends import gui

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the desktop window test runs on Windows only")

WATCHDOG_S = 90
CONNECT_WAIT_S = 45


def _unavailable(reason: str) -> None:
    """Fail on CI, where the backend must exist; skip anywhere else."""
    if os.environ.get("CI"):
        pytest.fail(f"the window backend should exist on CI: {reason}")
    pytest.skip(reason)


def _backend_problem() -> str | None:
    try:
        import webview  # noqa: F401
    except Exception as error:
        return f"pywebview does not load: {error}"
    try:
        import clr  # noqa: F401  (pythonnet: the .NET bridge the WebView2 backend uses)
    except Exception as error:
        return f"pythonnet does not load: {error}"
    return None


def test_the_window_opens_the_page_and_the_page_connects(tmp_path):
    problem = _backend_problem()
    if problem is not None:
        _unavailable(problem)
    seen: dict = {}

    def probe(window):
        watchdog = threading.Timer(WATCHDOG_S, _destroy, args=(window,))
        watchdog.start()
        try:
            deadline = time.monotonic() + CONNECT_WAIT_S
            status = ""
            while time.monotonic() < deadline:
                try:
                    status = window.evaluate_js("(document.getElementById('status') || {}).textContent || ''")
                except Exception:
                    status = ""  # the page is still loading
                if status == "Connected":
                    break
                time.sleep(0.5)
            seen["status"] = status
            seen["title"] = window.evaluate_js("document.title")
            seen["workspace"] = window.evaluate_js("(document.getElementById('workspace') || {}).textContent || ''")
            seen["prompt_enabled"] = window.evaluate_js("!(document.getElementById('prompt') || {disabled: true}).disabled")
            seen["has_fragment"] = window.evaluate_js("location.hash")
        except Exception as error:  # reported by the assertions below
            seen["error"] = repr(error)
        finally:
            watchdog.cancel()
            _destroy(window)

    settings = Settings(workspace=tmp_path)
    errors = io.StringIO()
    opener = functools.partial(gui.show_window, on_shown=probe)
    code = gui.run(settings, config_file=tmp_path / "missing-config.toml", client=FakeOllama([]),
                   errors=errors, open_window=opener)

    if "error" not in seen and "status" not in seen:
        _unavailable(f"the window did not open: {errors.getvalue().strip()}")
    assert "error" not in seen, seen
    assert code == 0, errors.getvalue()
    assert seen["status"] == "Connected", seen
    assert seen["title"] == "local-agent", seen
    assert Path(seen["workspace"]).resolve() == tmp_path.resolve(), seen
    assert seen["prompt_enabled"] is True, seen
    assert seen["has_fragment"] in ("", None), seen  # the page removed the token from the address bar


def _destroy(window) -> None:
    with contextlib.suppress(Exception):  # already closed: nothing to do
        window.destroy()
