"""Entry script of the windowed exe, local-agent-gui.exe (T18): `local-agent gui` with no console window.

It runs the same code as `local-agent gui` (frontends/gui.py, run_windowed). Its output goes to gui.log in the
data folder, and a failure to open the window shows a message box. Built next to local-agent.exe, sharing its files.

An import that fails before that code runs (for example a missing DLL in the bundle) would otherwise end with no
trace, since there is no console. So the import is guarded here: the traceback goes to gui.log and a message box
says where it is.
"""

import os
import sys
import traceback


def _report_startup_failure(text: str) -> None:
    folder = os.environ.get("LOCAL_AGENT_DATA_DIR") or os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "local-agent")
    log = os.path.join(folder, "gui.log")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(log, "w", encoding="utf-8") as stream:
            stream.write(text)
    except OSError:
        log = None
    if sys.platform == "win32":
        try:
            import ctypes

            where = f" Details are in {log}." if log else ""
            ctypes.windll.user32.MessageBoxW(None, f"local-agent could not start.{where}", "local-agent", 0x10)
        except Exception:  # no message box: the log (if written) is the only trace
            pass


if __name__ == "__main__":
    try:
        from local_agent.frontends import gui
    except BaseException:
        _report_startup_failure(traceback.format_exc())
        raise SystemExit(1) from None
    gui.run_windowed(sys.argv[1:])
