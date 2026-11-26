"""Smoke-test plugin for packaging\\smoke-exe.ps1. It is copied into a temp plugin folder, not run from the repo.

It imports the standard-library modules that plugins may use and the window backend (T18), records each result in the JSON file named by the
SMOKE_RESULTS environment variable, and registers one tool so the plugin is not left out for having no tools.
"""

import importlib
import json
import os

from local_agent.plugin import Risk, tool

MODULES = [
    "sqlite3",
    "ctypes",
    "bz2",
    "lzma",
    "xml.etree.ElementTree",
    "xml.dom.minidom",
    "csv",
    "decimal",
    "zipfile",
    "unittest",
]

# The desktop window (T18): pywebview's Windows backend loads pythonnet and the .NET runtime when it is imported.
# Importing it opens no window.
WINDOW_MODULES = ["webview", "clr", "webview.platforms.winforms"]

RESULTS: dict[str, str] = {}
for _name in MODULES + WINDOW_MODULES:
    try:
        importlib.import_module(_name)
        RESULTS[_name] = "ok"
    except Exception as e:  # any failure is a result to report, not a crash
        RESULTS[_name] = f"{type(e).__name__}: {e}"

with open(os.environ["SMOKE_RESULTS"], "w", encoding="utf-8") as _f:
    json.dump(RESULTS, _f, indent=2)


@tool(risk=Risk.READ)
def smoke_ping() -> str:
    """Return a fixed string. Used only by the release smoke test."""
    return "pong"
