"""The real desktop window (T18): pywebview's Edge WebView2 window on Windows.

Two cases, each showing a real window for a few seconds:
- the page loads and connects: `gui.run` with the window's probe, which waits for "Connected" (the page loaded,
  `/info` answered, the WebSocket opened with the window's own origin), reads the page state, and closes the window;
- Ctrl+C in a console closes the window and the command exits 0: the test starts `gui` in a child process with its
  own console, sends CTRL_C_EVENT to that console from a helper process (so this test process is not touched), and
  waits for the child to exit. The child is killed by its handle if it does not exit in time.

Skip or fail? A missing desktop session (no pywebview or pythonnet, no WebView2 runtime, no window) skips the test,
unless the environment variable WILDCAT_CODE_AGENT_REQUIRE_WINDOW is 1: then it fails. A window that opens and does not
behave (the page never connects, or Ctrl+C does not close it) fails in every case: that is a real bug.
"""

import functools
import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fakes import FakeOllama
from wildcat_code_agent.config import Settings
from wildcat_code_agent.frontends import gui

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the desktop window test runs on Windows only")

REQUIRE_WINDOW = os.environ.get("WILDCAT_CODE_AGENT_REQUIRE_WINDOW") == "1"
CONNECT_WAIT_S = 45
CHILD_READY_WAIT_S = 60
CHILD_EXIT_WAIT_S = 60
CTRL_C_HELPER = """
import ctypes, sys
k = ctypes.WinDLL("kernel32")
k.FreeConsole()  # leave the test's console
if not k.AttachConsole(int(sys.argv[1])):  # the child's own console
    sys.exit(3)
k.SetConsoleCtrlHandler(None, True)  # this helper ignores the event it sends
if not k.GenerateConsoleCtrlEvent(0, 0):  # CTRL_C_EVENT to every process on that console
    sys.exit(4)
"""
CHILD = """
import sys
from functools import partial
from pathlib import Path
from wildcat_code_agent.config import Settings
from wildcat_code_agent.frontends import gui

folder = Path(sys.argv[1])

def ready(window):
    print("READY", flush=True)  # printed after the console handler is installed

code = gui.run(Settings(workspace=folder, host="http://127.0.0.1:1", plugins_enabled=False),
               config_file=folder / "config.toml",
               open_window=partial(gui.show_window, on_shown=ready))
sys.exit(code)
"""


def _unavailable(reason: str) -> None:
    """Skip, or fail when WILDCAT_CODE_AGENT_REQUIRE_WINDOW=1 (a CI job that must have a desktop)."""
    if REQUIRE_WINDOW:
        pytest.fail(f"the window backend or desktop is required here: {reason}")
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


def _destroy(window) -> None:
    import contextlib

    with contextlib.suppress(Exception):  # already closed: nothing to do
        window.destroy()


def test_the_window_opens_the_page_and_the_page_connects(tmp_path):
    problem = _backend_problem()
    if problem is not None:
        _unavailable(problem)
    seen: dict = {}

    def probe(window):
        watchdog = threading.Timer(90, _destroy, args=(window,))
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

    errors = io.StringIO()
    code = gui.run(Settings(workspace=tmp_path), config_file=tmp_path / "missing-config.toml",
                   client=FakeOllama([]), errors=errors,
                   open_window=functools.partial(gui.show_window, on_shown=probe))

    if "status" not in seen and "error" not in seen:  # the probe never ran: no window opened
        _unavailable(f"the window did not open (exit {code}): {errors.getvalue().strip()}")
    assert "error" not in seen, seen
    assert code == 0, errors.getvalue()
    assert seen["status"] == "Connected", seen
    assert seen["title"] == "Wildcat Code Agent", seen
    assert Path(seen["workspace"]).resolve() == tmp_path.resolve(), seen
    assert seen["prompt_enabled"] is True, seen
    assert seen["has_fragment"] in ("", None), seen  # the page removed the token from the address bar


def test_ctrl_c_in_a_console_closes_the_window_and_exits_0(tmp_path):
    problem = _backend_problem()
    if problem is not None:
        _unavailable(problem)
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    out_path = tmp_path / "child-out.txt"
    err_path = tmp_path / "child-err.txt"
    with open(out_path, "w", encoding="utf-8") as out, open(err_path, "w", encoding="utf-8") as err:
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(tmp_path)],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=subprocess.CREATE_NEW_CONSOLE,  # not a new process group: that group ignores Ctrl+C
        )
    try:
        deadline = time.monotonic() + CHILD_READY_WAIT_S
        ready = False
        while time.monotonic() < deadline and not ready:
            if child.poll() is not None:
                break
            ready = "READY" in out_path.read_text(encoding="utf-8", errors="replace")
            if not ready:
                time.sleep(0.25)
        if not ready:
            code = child.poll()
            reason = err_path.read_text(encoding="utf-8", errors="replace").strip()
            if code is not None and ("could not open" in reason or "did not load" in reason):
                _unavailable(f"the window did not open in the child (exit {code}): {reason}")
            pytest.fail(f"the child window never reported ready (exit {code}): {reason}")

        helper = subprocess.run([sys.executable, "-c", CTRL_C_HELPER, str(child.pid)],
                                capture_output=True, timeout=60)
        assert helper.returncode == 0, helper.stderr.decode("utf-8", "replace")
        try:
            code = child.wait(timeout=CHILD_EXIT_WAIT_S)
        except subprocess.TimeoutExpired:
            child.kill()  # our own child, by its handle
            child.wait()
            pytest.fail("Ctrl+C did not close the window within "
                        f"{CHILD_EXIT_WAIT_S} s: " + err_path.read_text(encoding="utf-8", errors="replace"))
        assert code == 0, err_path.read_text(encoding="utf-8", errors="replace")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
