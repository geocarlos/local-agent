"""The session store (T17): one JSON file per session. Round trip, atomic writes, corrupt files, limits, and the folder."""

import json
import os
from pathlib import Path

import pytest

from local_agent import atomic, sessions
from local_agent.sessions import SessionError, SessionStore

SID = "sess-" + "a" * 32
OTHER = "sess-" + "b" * 32
MESSAGES = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "  Hello   there\nfriend  "},
    {"role": "assistant", "content": "Hi."},
]


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


def _save(store: SessionStore, sid: str = SID, messages=None, cwd: str = "C:\\work\\a", model: str = "m:1") -> None:
    store.save(session_id=sid, cwd=cwd, model=model, created="2026-11-25T10:00:00+00:00",
               messages=MESSAGES if messages is None else messages)


def test_a_saved_session_loads_back_the_same(store):
    _save(store)
    record = store.load(SID)
    assert record.session_id == SID
    assert record.cwd == "C:\\work\\a"
    assert record.model == "m:1"
    assert record.created == "2026-11-25T10:00:00+00:00"
    assert record.messages == MESSAGES
    assert record.updated.endswith("+00:00")


def test_the_title_is_the_first_user_message_trimmed(store):
    _save(store)
    assert store.load(SID).title == "Hello there friend"


def test_a_long_title_is_cut_to_80_characters(store):
    long = "word " * 40
    _save(store, messages=[{"role": "user", "content": long}])
    title = store.load(SID).title
    assert len(title) == sessions.TITLE_CHARS
    assert title.endswith("...")


def test_the_file_is_ascii_json_with_the_documented_fields(store):
    _save(store, messages=[{"role": "user", "content": "café"}])
    data = json.loads((store.folder / f"{SID}.json").read_text(encoding="ascii"))
    assert data["format"] == sessions.FORMAT
    assert set(data) == {"format", "id", "cwd", "model", "title", "created", "updated", "messages"}
    assert data["messages"][0]["content"] == "café"


def test_reasoning_text_is_not_saved(store):
    class FakeMessage:  # shaped like the client's pydantic message
        def model_dump(self, exclude_none=False):
            return {"role": "assistant", "content": "answer", "thinking": "secret reasoning"}

    _save(store, messages=[{"role": "user", "content": "q"}, FakeMessage()])
    assert store.load(SID).messages[1] == {"role": "assistant", "content": "answer"}


def test_a_missing_session_is_a_session_error(store):
    with pytest.raises(SessionError):
        store.load(SID)


@pytest.mark.parametrize("bad_id", ["../evil", "sess-XYZ", "sess-" + "a" * 31, "", None, "sess-" + "A" * 32])
def test_a_session_id_that_is_not_ours_never_reaches_the_file_system(store, bad_id):
    with pytest.raises(SessionError):
        store.load(bad_id)
    with pytest.raises(SessionError):
        store.save(session_id=bad_id, cwd="C:\\x", model="m", created="t", messages=MESSAGES)
    assert not store.folder.exists() or not list(store.folder.iterdir())


def test_a_crash_during_the_write_leaves_the_old_file(store, monkeypatch):
    _save(store, messages=[{"role": "user", "content": "first"}])
    before = (store.folder / f"{SID}.json").read_text(encoding="utf-8")

    def crash(*args, **kwargs):
        raise OSError("disk unplugged")

    monkeypatch.setattr(atomic.os, "replace", crash)
    with pytest.raises(SessionError):
        _save(store, messages=[{"role": "user", "content": "second"}])
    monkeypatch.undo()
    assert (store.folder / f"{SID}.json").read_text(encoding="utf-8") == before
    assert store.load(SID).messages[0]["content"] == "first"
    assert not list(store.folder.glob("*.tmp")), "the temp file is removed after a failed write"


def test_a_failed_check_leaves_the_target_alone(tmp_path):
    target = tmp_path / "file.json"
    target.write_text("old", encoding="utf-8")

    def refuse(temp: Path) -> None:
        raise ValueError("the new text is wrong")

    with pytest.raises(ValueError):
        atomic.atomic_write_text(target, "new", check=refuse)
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["file.json"]


def test_a_corrupt_file_is_skipped_with_a_warning_and_the_list_goes_on(tmp_path):
    logged: list[str] = []
    store = SessionStore(tmp_path / "sessions", log=logged.append)
    _save(store, SID, messages=[{"role": "user", "content": "good"}])
    store.folder.mkdir(exist_ok=True)
    (store.folder / f"{OTHER}.json").write_text("{not json", encoding="utf-8")
    (store.folder / f"sess-{'c' * 32}.json").write_text(json.dumps({"format": 1, "id": "sess-" + "d" * 32}),
                                                        encoding="utf-8")  # the id does not match the name

    listed = store.list_sessions()
    assert [s.session_id for s in listed] == [SID]
    assert len(logged) == 2 and all(line.startswith("Warning: skipped a saved session") for line in logged)
    with pytest.raises(SessionError):
        store.load(OTHER)


@pytest.mark.parametrize("payload", [
    {"format": 2, "id": SID},
    {"format": 1, "id": SID, "cwd": "", "model": "m", "created": "t", "updated": "t", "messages": []},
    {"format": 1, "id": SID, "cwd": "C:\\x", "model": "m", "created": "t", "updated": "t", "messages": "no"},
    {"format": 1, "id": SID, "cwd": "C:\\x", "model": "m", "created": "t", "updated": "t",
     "messages": [{"role": "robot", "content": "x"}]},
    {"format": 1, "id": SID, "cwd": "C:\\x", "model": "m", "created": "t", "updated": "t", "messages": [],
     "title": 5},
])
def test_files_that_fail_the_checks_are_refused(store, payload):
    store.folder.mkdir(parents=True, exist_ok=True)
    (store.folder / f"{SID}.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionError):
        store.load(SID)


def test_listing_is_newest_first(store):
    _save(store, SID)
    _save(store, OTHER)
    path = store.folder / f"{SID}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated"] = "2020-01-01T00:00:00+00:00"  # an older session, by its own timestamp
    path.write_text(json.dumps(data), encoding="utf-8")
    assert [s.session_id for s in store.list_sessions()] == [OTHER, SID]


def test_a_session_too_large_is_not_saved_and_the_file_stays(store, monkeypatch):
    _save(store, messages=[{"role": "user", "content": "small"}])
    monkeypatch.setattr(sessions, "MAX_SESSION_BYTES", 500)
    with pytest.raises(SessionError, match="larger than"):
        _save(store, messages=[{"role": "user", "content": "x" * 1000}])
    assert store.load(SID).messages[0]["content"] == "small"


def test_the_oldest_sessions_beyond_the_limit_are_deleted_with_a_warning(tmp_path, monkeypatch):
    logged: list[str] = []
    store = SessionStore(tmp_path / "sessions", log=logged.append)
    monkeypatch.setattr(sessions, "MAX_SAVED_SESSIONS", 2)
    ids = ["sess-" + str(i) * 32 for i in range(3)]
    for index, sid in enumerate(ids):
        _save(store, sid, messages=[{"role": "user", "content": f"n{index}"}])
        os.utime(store.folder / f"{sid}.json", (1_000_000 + index, 1_000_000 + index))
    _save(store, ids[2])  # the newest is re-saved: the limit then removes the oldest file
    remaining = {p.stem for p in store.folder.glob("sess-*.json")}
    assert remaining == {ids[1], ids[2]}
    assert any("deleted 1" in line for line in logged)


def test_the_folder_is_the_data_folder_override_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv(sessions.ENV_DATA_DIR, str(tmp_path / "data"))
    assert sessions.sessions_folder() == tmp_path / "data" / "sessions"


def test_without_the_override_the_folder_is_the_per_user_data_folder(monkeypatch):
    monkeypatch.delenv(sessions.ENV_DATA_DIR, raising=False)
    folder = sessions.sessions_folder()
    assert folder.name == "sessions"
    assert folder.parent.name == "local-agent"


def test_a_write_waits_while_another_handle_holds_the_target_open(tmp_path):
    """Windows refuses the rename while a reader has the file open; the write retries until it can replace it."""
    import threading
    import time

    target = tmp_path / "file.json"
    target.write_text("old", encoding="utf-8")
    release = threading.Event()
    opened = threading.Event()

    def hold_open() -> None:
        with open(target, encoding="utf-8") as handle:  # a reader keeps the file open
            opened.set()
            release.wait(5)

    reader = threading.Thread(target=hold_open)
    reader.start()
    opened.wait(5)
    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        atomic.atomic_write_text(target, "new")
    finally:
        timer.cancel()
        release.set()
        reader.join(5)
    assert target.read_text(encoding="utf-8") == "new"


def test_a_target_that_stays_locked_raises_after_the_retries(tmp_path, monkeypatch):
    target = tmp_path / "file.json"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(atomic.time, "sleep", lambda seconds: None)

    def locked(source, destination):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(atomic.os, "replace", locked)
    with pytest.raises(PermissionError):
        atomic.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize("calls", [
    [{"function": {"arguments": {}}}],                     # no name
    [{"function": {"name": 3, "arguments": {}}}],          # name not text
    ["read_file"],                                         # not an object
    [{"function": {"name": "x", "arguments": "oops"}}],    # arguments not an object
])
def test_a_malformed_tool_call_in_the_history_is_refused(store, calls):
    messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "", "tool_calls": calls}]
    store.folder.mkdir(parents=True, exist_ok=True)
    data = {"format": 1, "id": SID, "cwd": "C:/work", "model": "m", "title": None, "created": "t",
            "updated": "t", "messages": messages}
    (store.folder / f"{SID}.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SessionError):
        store.load(SID)
    assert store.list_sessions() == []
