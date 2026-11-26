"""Saved chat sessions (T17): one JSON file per session, in the per-user data folder.

Folder: `<data folder>\\sessions`, where the data folder is %APPDATA%\\wildcat-code-agent (platformdirs, roaming).
`WILDCAT_CODE_AGENT_DATA_DIR` replaces the data folder; the tests set it, so the real one is never touched. Nothing
is written into the install folder.

File format: JSON in UTF-8, `"format": 1`, with `id`, `cwd`, `model`, `title` (the first user message, trimmed
to TITLE_CHARS), `created` and `updated` (ISO 8601, UTC) and `messages` (the history the model sees: system,
user, assistant and tool messages). The model's reasoning text is not saved.

Writes are atomic (atomic.py). A file that cannot be parsed or checked is skipped by listing, with a warning,
and refused by loading, so one bad file never empties the list.

Limits (decided, documented in docs/protocol.md and README):
- a session over MAX_SESSION_BYTES is not saved: the save raises SessionError, the chat goes on, and the
  server logs a warning;
- at most MAX_SAVED_SESSIONS files are kept: after a save, the least recently updated files beyond that are
  deleted, and a warning says how many.
"""

import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import platformdirs

from wildcat_code_agent.atomic import atomic_write_text
from wildcat_code_agent.config import APP_NAME, ENV_DATA_DIR, LEGACY_APP_NAME, adopt_legacy_folder

FORMAT = 1
FOLDER_NAME = "sessions"
MAX_SESSION_BYTES = 5 * 1024 * 1024
MAX_SAVED_SESSIONS = 200
TITLE_CHARS = 80
ROLES = ("system", "user", "assistant", "tool")
SESSION_ID = re.compile(r"sess-[0-9a-f]{32}")
FILE_PATTERN = "sess-*.json"


class SessionError(Exception):
    """A saved session is missing, cannot be read or checked, or cannot be saved."""


def data_folder() -> Path:
    """The per-user data folder (WILDCAT_CODE_AGENT_DATA_DIR if set)."""
    override = os.environ.get(ENV_DATA_DIR, "").strip()
    if override:
        return Path(override)
    folder = Path(platformdirs.user_data_dir(APP_NAME, appauthor=False, roaming=True))
    legacy = Path(platformdirs.user_data_dir(LEGACY_APP_NAME, appauthor=False, roaming=True))
    return adopt_legacy_folder(folder, legacy)


def sessions_folder() -> Path:
    return data_folder() / FOLDER_NAME


def title_of(messages: list[dict]) -> str | None:
    """The title: the first user message, whitespace collapsed and cut to TITLE_CHARS. None if there is none."""
    for message in messages:
        if message.get("role") == "user":
            text = " ".join(str(message.get("content") or "").split())
            if text:
                return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 3] + "..."
    return None


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _plain_message(message: Any) -> dict:
    """A history message as a plain dict. The client's message objects are pydantic models; the reasoning
    text is dropped, as the file format says."""
    data = message.model_dump(exclude_none=True) if hasattr(message, "model_dump") else dict(message)
    data.pop("thinking", None)
    return data


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    cwd: str
    model: str
    title: str | None
    created: str
    updated: str
    messages: list[dict]


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    cwd: str
    model: str
    title: str | None
    updated: str


class SessionStore:
    """Saves, loads and lists sessions in one folder. Safe to share between threads."""

    def __init__(self, folder: Path, *, log: Callable[[str], None] = lambda line: None) -> None:
        self.folder = folder
        self._log = log
        self._lock = threading.Lock()

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise SessionError("not a session id")  # also keeps a client's id out of the file path
        return self.folder / f"{session_id}.json"

    def save(self, *, session_id: str, cwd: str, model: str, created: str, messages: list[dict]) -> None:
        """Write the session's file (atomically). Raises SessionError if it is too large or cannot be written."""
        plain = [_plain_message(m) for m in messages]
        data = {"format": FORMAT, "id": session_id, "cwd": cwd, "model": model, "title": title_of(plain),
                "created": created, "updated": now_iso(), "messages": plain}
        text = json.dumps(data, ensure_ascii=True, indent=1)
        if len(text) > MAX_SESSION_BYTES:  # ASCII-only, so characters are bytes
            raise SessionError(f"the session is larger than {MAX_SESSION_BYTES // (1024 * 1024)} MiB; it was not saved")
        path = self._path(session_id)
        with self._lock:
            try:
                atomic_write_text(path, text)
            except OSError as e:
                raise SessionError(f"cannot write the session file: {e}") from None
            self._prune(keep=path)

    def load(self, session_id: str) -> SessionRecord:
        path = self._path(session_id)
        with self._lock:  # not while a save is replacing the file
            if not path.is_file():
                raise SessionError("no saved session with that id")
            return _parse(path, session_id)

    def list_sessions(self) -> list[SessionSummary]:
        """Every readable session, most recently updated first. Unreadable files are skipped with a warning."""
        summaries: list[SessionSummary] = []
        with self._lock:
            files = self._files()
            for path in files:
                try:
                    record = _parse(path, path.stem)
                except SessionError as e:
                    self._log(f"Warning: skipped a saved session that cannot be read: {path.name} ({e})")
                    continue
                summaries.append(SessionSummary(record.session_id, record.cwd, record.model, record.title,
                                                record.updated))
        summaries.sort(key=lambda s: s.updated, reverse=True)
        return summaries

    def _files(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.glob(FILE_PATTERN) if p.is_file())

    def _prune(self, *, keep: Path) -> None:
        files = self._files()
        if len(files) <= MAX_SAVED_SESSIONS:
            return
        extra = len(files) - MAX_SAVED_SESSIONS
        by_age = sorted((p for p in files if p != keep), key=lambda p: p.stat().st_mtime)
        deleted = 0
        for path in by_age[:extra]:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                continue
        self._log(f"Warning: more than {MAX_SAVED_SESSIONS} saved sessions; deleted {deleted} of the least "
                  "recently used (see docs/protocol.md).")


def _parse(path: Path, expected_id: str) -> SessionRecord:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SessionError(f"cannot read it: {type(e).__name__}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise SessionError("not a session file of this version")
    session_id = data.get("id")
    if session_id != expected_id or not SESSION_ID.fullmatch(str(session_id)):
        raise SessionError("the id does not match the file name")
    cwd, model, created, updated = (data.get(k) for k in ("cwd", "model", "created", "updated"))
    if not all(isinstance(v, str) and v for v in (cwd, model, created, updated)):
        raise SessionError("a required field is missing")
    title = data.get("title")
    if title is not None and not isinstance(title, str):
        raise SessionError("the title is not text")
    messages = data.get("messages")
    if not isinstance(messages, list) or not all(
            isinstance(m, dict) and m.get("role") in ROLES and isinstance(m.get("content", ""), str)
            for m in messages):
        raise SessionError("the messages are not a valid history")
    for message in messages:  # replay reads these fields, so a malformed call is refused here, not on load
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise SessionError("a tool call in the history has no function name")
            arguments = function.get("arguments", {})
            if arguments is not None and not isinstance(arguments, dict):
                raise SessionError("a tool call in the history has bad arguments")
    return SessionRecord(session_id=session_id, cwd=cwd, model=model, title=title, created=created,
                         updated=updated, messages=messages)
