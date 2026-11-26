"""The desktop window: `local-agent gui` (T18).

It runs the T15 web server in this process (`web.WebServer`: 127.0.0.1, a free port, a token made for this
run), waits until it listens, and opens the T16 page in a pywebview window at `http://127.0.0.1:PORT/#token=...`.
The page is the same one the browser gets, so chat, tool cards, approvals and sessions work the same way.
Closing the window stops the server (the same shutdown as `serve --web`) and `gui` exits with code 0.

Decided (T18, see TASKS.md):
- The token goes only into the window's address. It is never printed: the window is the way in, and the
  console (or the gui.log file of the windowed exe) does not need it.
- On Windows the window is the Edge WebView2 control (`gui="edgechromium"`). That is set explicitly so a
  missing WebView2 runtime is an error, not a silent fall back to the old engine.
- The window is opened through `open_window`, so tests can replace it with a fake. A window that cannot be
  made is an error with exit code 1, and the server is stopped.
"""

import contextlib
import os
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from local_agent import sessions
from local_agent.config import Settings
from local_agent.frontends import web

WINDOW_TITLE = "local-agent"
WINDOW_SIZE = (1100, 780)
WINDOW_MIN_SIZE = (420, 480)
GUI_LOG_NAME = "gui.log"  # the windowed exe's stderr (it has no console), in the data folder; rewritten on each start
WEBVIEW2_HINT = ("The window needs the Microsoft Edge WebView2 runtime. It comes with Windows 11 and most "
                 "Windows 10 installs; install it from Microsoft if the window does not open.")


class WindowError(RuntimeError):
    """The window could not be opened or failed while it was open."""


def show_window(url: str, *, on_shown: Callable[[Any], None] | None = None) -> None:
    """Open a window at `url` and return when it is closed. `on_shown` runs on its own thread with the window
    object once the window exists (the tests use it to check the page). Raises WindowError when no window can
    be made: the backend is missing or the WebView2 runtime is not installed."""
    try:
        import webview  # imported here: the window stack is not needed by chat, serve or the tests of other modules
    except Exception as e:
        raise WindowError(f"the window backend (pywebview) did not load: {e}") from e
    gui = "edgechromium" if sys.platform == "win32" else None
    try:
        window = webview.create_window(WINDOW_TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                                       min_size=WINDOW_MIN_SIZE)
        if on_shown is not None:
            threading.Thread(target=on_shown, args=(window,), name="gui-check", daemon=True).start()
        webview.start(gui=gui)
    except Exception as e:
        hint = f" {WEBVIEW2_HINT}" if sys.platform == "win32" else ""
        raise WindowError(f"the window could not open: {e}.{hint}") from e


def run(settings: Settings, *, config_file: Path | None = None, client: Any = None,
        store: sessions.SessionStore | None = None, errors: TextIO | None = None,
        open_window: Callable[[str], None] = show_window) -> int:
    """Start the server, open the window, and stop the server when the window closes. Returns the exit code:
    0 when the window was closed, 1 when the server or the window could not start."""
    errors = sys.stderr if errors is None else errors

    def log(line: str) -> None:
        print(line, file=errors, flush=True)

    try:
        server = web.WebServer(settings, port=0, client=client, log=log, store=store, config_file=config_file)
    except OSError as e:
        log(f"Error: cannot listen on 127.0.0.1: {e}")
        return 1
    try:
        server.start()
    except web.ServerStartError as e:
        log(f"Error: {e}")
        return 1
    try:
        log(f"Opening the local-agent window on {server.url} (close the window to stop).")
        open_window(server.link)
    except WindowError as e:
        log(f"Error: {e}")
        return 1
    except KeyboardInterrupt:  # Ctrl+C in the console closes the window the same way
        pass
    finally:
        server.stop()
    return 0


def ensure_streams(data_folder: Path | None = None) -> Path | None:
    """For the windowed exe, which has no console: sys.stdout and sys.stderr are None there, so anything that
    prints would fail. stdout goes nowhere, and stderr goes to gui.log in the data folder (rewritten on each
    start), so a failed start leaves a trace. Returns the log path when the streams were replaced, else None."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        folder = sessions.data_folder() if data_folder is None else data_folder
        path = folder / GUI_LOG_NAME
        try:
            folder.mkdir(parents=True, exist_ok=True)
            sys.stderr = open(path, "w", encoding="utf-8", buffering=1)
        except OSError:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
            return None
        return path
    return None


def show_error_box(text: str) -> None:
    """A message box for the windowed exe, where nothing else is visible. Windows only."""
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, WINDOW_TITLE, 0x10)  # MB_ICONERROR


def start_folder_for_windowed() -> None:
    """The Start menu shortcut starts in the program folder, and the workspace defaults to the current folder,
    so the agent would work beside local-agent.exe. When the current folder is the program's (or inside it),
    move to Documents, or the profile when there is no Documents folder. Other folders are left alone, and a
    `--cwd` flag or a `workspace` in the config file still wins over this default."""
    if not getattr(sys, "frozen", False):
        return
    program = Path(sys.executable).resolve().parent
    here = Path.cwd().resolve()
    if here != program and program not in here.parents:
        return
    documents = Path.home() / "Documents"
    os.chdir(documents if documents.is_dir() else Path.home())


def run_windowed(argv: list[str]) -> None:
    """The entry of the windowed exe (packaging/gui_launcher.py): `local-agent gui` with a message box for a
    failure, since nothing else is shown. Exits with the code of `main`."""
    from local_agent import main

    log_path = ensure_streams()
    start_folder_for_windowed()
    try:
        main(["gui", *argv])
    except SystemExit as e:
        if log_path is not None and e.code not in (0, None):
            show_error_box(f"local-agent could not open its window. Details are in {log_path}.")
        raise
    except Exception:
        if log_path is not None:
            traceback.print_exc()
            show_error_box(f"local-agent stopped with an error. Details are in {log_path}.")
        raise
    finally:
        with contextlib.suppress(Exception):
            sys.stdout.flush()
