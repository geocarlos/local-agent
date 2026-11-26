"""The static files of the web UI (T16): the page, its CSS and its JavaScript, in `src/wildcat_code_agent/web/`.

Only files with an allowed suffix inside that folder are served. A URL path with `..`, a backslash, a drive
letter, an empty or `.` segment, a NUL, a symbolic link out of the folder, or a folder is not a file here, so
the route answers 404. The folder is found in the source tree, or under `sys._MEIPASS` in the frozen build
(the spec copies it to `wildcat_code_agent/web`).

The page itself holds no secret. Its Content-Security-Policy allows scripts and styles from this server only,
and `connect-src` only this server's WebSocket on the two loopback names.
"""

import sys
from pathlib import Path

WEB_FOLDER = "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
INDEX = "index.html"


def web_root() -> Path:
    """The web folder: beside this package in the source tree, or in the PyInstaller bundle."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "wildcat_code_agent" / WEB_FOLDER  # noqa: SLF001 (PyInstaller's own attribute)
    return Path(__file__).resolve().parent.parent / WEB_FOLDER


def find_asset(name: str, root: Path | None = None) -> Path | None:
    """The file for a URL path without its leading slash ("" is the page). None when it is not a served file."""
    base = (web_root() if root is None else root).resolve()
    if name in ("", "/"):
        name = INDEX
    if not name or "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
        return None
    if any(part in ("", ".", "..") for part in name.split("/")):
        return None
    if Path(name).suffix.lower() not in CONTENT_TYPES:
        return None
    try:
        resolved = (base / name).resolve(strict=True)
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or not resolved.is_relative_to(base):
        return None
    return resolved


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def content_security_policy(port: int) -> str:
    """The policy for every page of this server. Scripts and styles only from this origin; the WebSocket only
    to this server's own loopback address and name on its port."""
    ws = f"ws://127.0.0.1:{port} ws://localhost:{port}"
    return "; ".join([
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        f"connect-src 'self' {ws}",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ])


def page_headers(port: int) -> dict[str, str]:
    """The headers on every response of the web UI."""
    return {
        "Content-Security-Policy": content_security_policy(port),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
