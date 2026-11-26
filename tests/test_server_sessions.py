"""Saved sessions, the model choice and the settings editor over the stdio server (T17), in-process.

Each test runs the server on its own thread (test_server.Wire) with the fake Ollama client and a temp store and
config file. A "restart" is a new server on the same store folder.
"""

import shutil
from pathlib import Path

import pytest

from fakes import FakeOllama, text_reply, tool_reply
from local_agent import config, sessions
from local_agent.config import Settings
from local_agent.frontends import server as acp
from local_agent.frontends.server import EXT_MODELS, EXT_SETTINGS_GET, EXT_SETTINGS_SET
from local_agent.sessions import SessionStore
from test_server import Wire, initialize, new_session, prompt, updates

INVALID_PARAMS = -32602
NOT_FOUND = -32002
INVALID_REQUEST = -32600
INTERNAL = -32603


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "data" / "sessions")


def _wire(workspace: Path, responses: list, store: SessionStore, config_file: Path, **kwargs) -> Wire:
    return Wire(workspace, responses, store=store, config_file=config_file, **kwargs)


def _call(w: Wire, method: str, params: dict) -> dict:
    """One request; returns its response message (result or error)."""
    return w.read_until(w.request(method, params))[-1]


@pytest.fixture
def config_file(tmp_path) -> Path:
    return tmp_path / "config" / "config.toml"


@pytest.fixture
def workspace(tmp_path) -> Path:
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "notes.txt").write_text("alpha beta\n", encoding="utf-8")
    return folder


# ----- capabilities and session/new --------------------------------------------------------------

def test_initialize_advertises_load_and_list(tmp_path, workspace, config_file):
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        caps = initialize(w)["result"]["agentCapabilities"]
        assert caps["loadSession"] is True
        assert caps["sessionCapabilities"] == {"list": {}}
    finally:
        w.close()


def test_session_new_answers_the_model_option_with_the_installed_models(tmp_path, workspace, config_file):
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        result = _call(w, "session/new", {"cwd": str(workspace), "mcpServers": []})["result"]
        option = result["configOptions"][0]
        assert option["id"] == "model" and option["category"] == "model" and option["type"] == "select"
        assert option["currentValue"] == Settings().model
        values = [o["value"] for o in option["options"]]
        assert set(values) == {"qwen3-vl:8b", "qwen2.5-coder:7b"}
        assert result["_meta"]["model"] == {"name": "qwen3-vl:8b", "tools": True}
        assert "warnings" not in result["_meta"]
    finally:
        w.close()


def test_a_model_without_tools_gets_a_warning_on_session_new(tmp_path, workspace, config_file):
    client = FakeOllama([], capabilities=("completion",))
    w = _wire(workspace, [], _store(tmp_path), config_file, client=client)
    try:
        initialize(w)
        result = _call(w, "session/new", {"cwd": str(workspace), "mcpServers": []})["result"]
        assert result["_meta"]["model"]["tools"] is False
        assert any("'tools' capability" in text for text in result["_meta"]["warnings"])
    finally:
        w.close()


def test_session_new_still_works_when_ollama_is_down(tmp_path, workspace, config_file):
    client = FakeOllama([], list_error=ConnectionError("refused"), show_error=ConnectionError("refused"))
    w = _wire(workspace, [], _store(tmp_path), config_file, client=client)
    try:
        initialize(w)
        response = _call(w, "session/new", {"cwd": str(workspace), "mcpServers": []})
        result = response["result"]
        assert result["sessionId"].startswith("sess-")
        assert [o["value"] for o in result["configOptions"][0]["options"]] == [Settings().model]
        assert any("Cannot reach Ollama" in text for text in result["_meta"]["warnings"])
    finally:
        w.close()


def test_a_silent_ollama_does_not_hang_session_new(tmp_path, workspace, config_file, monkeypatch):
    import threading
    monkeypatch.setattr(acp, "OLLAMA_CALL_TIMEOUT_S", 0.2)
    release = threading.Event()

    class Silent(FakeOllama):
        def list(self):
            release.wait(5)  # black hole until the test ends
            return super().list()

    w = _wire(workspace, [], _store(tmp_path), config_file, client=Silent([]))
    try:
        initialize(w)
        result = _call(w, "session/new", {"cwd": str(workspace), "mcpServers": []})["result"]
        assert any("did not answer within" in text or "Cannot reach Ollama" in text
                   for text in result["_meta"]["warnings"])
    finally:
        release.set()
        w.close()


# ----- saving, listing and reopening -------------------------------------------------------------

def test_a_turn_saves_the_session_and_the_list_shows_it(tmp_path, workspace, config_file):
    store = _store(tmp_path)
    w = _wire(workspace, [text_reply("Hi there.")], store, config_file)
    try:
        initialize(w)
        sid = new_session(w, workspace)
        assert _call(w, "session/list", {})["result"]["sessions"] == []  # nothing saved before a turn
        prompt(w, sid, "  Say   hi  ")
        listed = _call(w, "session/list", {})["result"]["sessions"]
        assert [s["sessionId"] for s in listed] == [sid]
        assert listed[0]["title"] == "Say hi"
        assert listed[0]["cwd"] == str(workspace)
        assert listed[0]["_meta"]["model"] == Settings().model
        assert store.load(sid).messages[-1] == {"role": "assistant", "content": "Hi there."}
    finally:
        w.close()


def test_a_session_reopens_after_a_restart_with_its_history_replayed_and_continued(tmp_path, workspace, config_file):
    store = _store(tmp_path)
    first = _wire(workspace, [text_reply("Paris.")], store, config_file)
    try:
        initialize(first)
        sid = new_session(first, workspace)
        prompt(first, sid, "Capital of France?")
    finally:
        first.close()

    client = FakeOllama([text_reply("It is on the Seine.")])
    second = _wire(workspace, [], store, config_file, client=client)
    try:
        initialize(second)
        request_id = second.request("session/load", {"sessionId": sid, "cwd": str(workspace), "mcpServers": []})
        messages = second.read_until(request_id)
        replay = updates(messages)
        user_chunks = [u for u in replay if u["sessionUpdate"] == "user_message_chunk"]
        agent_chunks = [u for u in replay if u["sessionUpdate"] == "agent_message_chunk"]
        assert [u["content"]["text"] for u in user_chunks] == ["Capital of France?"]
        assert [u["content"]["text"] for u in agent_chunks] == ["Paris."]
        assert all(m["params"]["sessionId"] == sid for m in messages if m.get("method") == "session/update")
        result = messages[-1]["result"]
        assert result["configOptions"][0]["currentValue"] == Settings().model

        prompt(second, sid, "And the river?")
        sent = client.calls[-1]["messages"]
        assert [m["content"] for m in sent if m["role"] == "user"] == ["Capital of France?", "And the river?"]
        assert store.load(sid).messages[-1]["content"] == "It is on the Seine."
    finally:
        second.close()


def test_a_replayed_tool_call_shows_as_a_completed_call(tmp_path, workspace, config_file):
    store = _store(tmp_path)
    first = _wire(workspace, [tool_reply(("read_file", {"path": "notes.txt"})), text_reply("Read it.")], store,
                  config_file)
    try:
        initialize(first)
        sid = new_session(first, workspace)
        prompt(first, sid, "read notes")
    finally:
        first.close()

    second = _wire(workspace, [], store, config_file)
    try:
        initialize(second)
        request_id = second.request("session/load", {"sessionId": sid, "cwd": str(workspace), "mcpServers": []})
        replay = updates(second.read_until(request_id))
        calls = [u for u in replay if u["sessionUpdate"] == "tool_call"]
        results = [u for u in replay if u["sessionUpdate"] == "tool_call_update"]
        assert calls[0]["title"].startswith("read_file(")
        assert calls[0]["kind"] == "read"
        assert results[0]["toolCallId"] == calls[0]["toolCallId"]
        assert results[0]["status"] == "completed"
        assert "alpha beta" in results[0]["content"][0]["content"]["text"]
    finally:
        second.close()


def test_session_list_pages_and_filters_by_folder(tmp_path, workspace, config_file, monkeypatch):
    monkeypatch.setattr(acp, "LIST_PAGE_SIZE", 2)
    store = _store(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    w = _wire(workspace, [text_reply(f"r{i}") for i in range(4)], store, config_file)
    try:
        initialize(w)
        for index in range(3):
            sid = new_session(w, workspace)
            prompt(w, sid, f"chat {index}")
        other_sid = new_session(w, other)
        prompt(w, other_sid, "elsewhere")
        first = _call(w, "session/list", {})["result"]
        assert len(first["sessions"]) == 2 and "nextCursor" in first
        second = _call(w, "session/list", {"cursor": first["nextCursor"]})["result"]
        assert len(second["sessions"]) == 2 and "nextCursor" not in second
        only_other = _call(w, "session/list", {"cwd": str(other)})["result"]["sessions"]
        assert [s["sessionId"] for s in only_other] == [other_sid]
        assert _call(w, "session/list", {"cursor": "nonsense"})["error"]["code"] == INVALID_PARAMS
    finally:
        w.close()


def test_loading_an_unknown_session_is_not_found(tmp_path, workspace, config_file):
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        response = _call(w, "session/load", {"sessionId": "sess-" + "f" * 32, "cwd": str(workspace), "mcpServers": []})
        assert response["error"]["code"] == NOT_FOUND
    finally:
        w.close()


def test_a_corrupt_saved_session_is_listed_as_absent_and_cannot_be_loaded(tmp_path, workspace, config_file):
    logged: list[str] = []
    store = SessionStore(tmp_path / "data" / "sessions", log=logged.append)
    w = _wire(workspace, [text_reply("ok")], store, config_file)
    try:
        initialize(w)
        good = new_session(w, workspace)
        prompt(w, good, "fine")
        bad = "sess-" + "c" * 32
        (store.folder / f"{bad}.json").write_text("{broken", encoding="utf-8")
        listed = [s["sessionId"] for s in _call(w, "session/list", {})["result"]["sessions"]]
        assert listed == [good]
        assert any("skipped a saved session" in line for line in logged)
        assert _call(w, "session/load", {"sessionId": bad, "cwd": str(workspace), "mcpServers": []})["error"]["code"] == NOT_FOUND
    finally:
        w.close()


def test_a_session_whose_folder_is_gone_fails_cleanly(tmp_path, workspace, config_file):
    store = _store(tmp_path)
    gone = tmp_path / "gone"
    gone.mkdir()
    first = _wire(workspace, [text_reply("ok")], store, config_file)
    try:
        initialize(first)
        sid = new_session(first, gone)
        prompt(first, sid, "remember this")
    finally:
        first.close()
    shutil.rmtree(gone)

    second = _wire(workspace, [], store, config_file)
    try:
        initialize(second)
        response = _call(second, "session/load", {"sessionId": sid, "cwd": str(gone), "mcpServers": []})
        assert response["error"]["code"] == INVALID_PARAMS
        assert "no longer exists" in response["error"]["message"]
        # Listing still shows the saved file (it is readable); opening it fails again, and nothing is half-loaded.
        assert sid in [s["sessionId"] for s in _call(second, "session/list", {})["result"]["sessions"]]
        again = _call(second, "session/load", {"sessionId": sid, "cwd": str(gone), "mcpServers": []})
        assert again["error"]["code"] == INVALID_PARAMS
    finally:
        second.close()


def test_loading_with_a_different_folder_is_refused(tmp_path, workspace, config_file):
    store = _store(tmp_path)
    first = _wire(workspace, [text_reply("ok")], store, config_file)
    try:
        initialize(first)
        sid = new_session(first, workspace)
        prompt(first, sid, "here")
    finally:
        first.close()
    second = _wire(workspace, [], store, config_file)
    try:
        initialize(second)
        response = _call(second, "session/load", {"sessionId": sid, "cwd": str(tmp_path), "mcpServers": []})
        assert response["error"]["code"] == INVALID_PARAMS
    finally:
        second.close()


def test_a_session_too_large_to_save_still_answers_the_turn(tmp_path, workspace, config_file, monkeypatch):
    monkeypatch.setattr(sessions, "MAX_SESSION_BYTES", 300)
    w = _wire(workspace, [text_reply("a fairly long reply " * 20)], _store(tmp_path), config_file)
    try:
        initialize(w)
        sid = new_session(w, workspace)
        _, messages = prompt(w, sid, "hi")
        assert messages[-1]["result"]["stopReason"] == "end_turn"
        assert any("was not saved" in line for line in w.log)
    finally:
        w.close()


# ----- the model choice --------------------------------------------------------------------------

def test_set_config_option_switches_the_model_of_this_session_only(tmp_path, workspace, config_file):
    client = FakeOllama([text_reply("one"), text_reply("two")])
    w = _wire(workspace, [], _store(tmp_path), config_file, client=client)
    try:
        initialize(w)
        sid = new_session(w, workspace)
        response = _call(w, "session/set_config_option",
                         {"sessionId": sid, "configId": "model", "value": "qwen2.5-coder:7b"})
        result = response["result"]
        assert result["configOptions"][0]["currentValue"] == "qwen2.5-coder:7b"
        assert result["_meta"]["model"] == {"name": "qwen2.5-coder:7b", "tools": True}
        prompt(w, sid, "use the new model")
        assert client.calls[-1]["model"] == "qwen2.5-coder:7b"
        other = new_session(w, workspace)
        prompt(w, other, "a second session keeps the default")
        assert client.calls[-1]["model"] == Settings().model
    finally:
        w.close()


def test_set_config_option_refuses_a_missing_model_and_a_bad_request(tmp_path, workspace, config_file):
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        sid = new_session(w, workspace)
        missing = _call(w, "session/set_config_option", {"sessionId": sid, "configId": "model", "value": "nope:1"})
        assert missing["error"]["code"] == INVALID_PARAMS and "not installed" in missing["error"]["message"]
        wrong_option = _call(w, "session/set_config_option", {"sessionId": sid, "configId": "mode", "value": "x"})
        assert wrong_option["error"]["code"] == INVALID_PARAMS
        empty = _call(w, "session/set_config_option", {"sessionId": sid, "configId": "model", "value": "  "})
        assert empty["error"]["code"] == INVALID_PARAMS
        unknown = _call(w, "session/set_config_option", {"sessionId": "sess-" + "0" * 32, "configId": "model",
                                                         "value": "qwen3-vl:8b"})
        assert unknown["error"]["code"] == NOT_FOUND
    finally:
        w.close()


def test_set_config_option_with_ollama_down_is_an_error_and_keeps_the_model(tmp_path, workspace, config_file):
    client = FakeOllama([])
    w = _wire(workspace, [], _store(tmp_path), config_file, client=client)
    try:
        initialize(w)
        sid = new_session(w, workspace)
        client.show_error = ConnectionError("refused")
        response = _call(w, "session/set_config_option", {"sessionId": sid, "configId": "model",
                                                          "value": "qwen2.5-coder:7b"})
        assert response["error"]["code"] == INTERNAL
        assert "Cannot reach Ollama" in response["error"]["message"]
        client.show_error = None
        result = _call(w, "session/new", {"cwd": str(workspace), "mcpServers": []})["result"]
        assert result["configOptions"][0]["currentValue"] == Settings().model
    finally:
        w.close()


def test_a_model_without_tools_gets_a_warning_when_chosen(tmp_path, workspace, config_file):
    client = FakeOllama([], capabilities=("completion",))
    w = _wire(workspace, [], _store(tmp_path), config_file, client=client)
    try:
        initialize(w)
        sid = new_session(w, workspace)
        result = _call(w, "session/set_config_option",
                       {"sessionId": sid, "configId": "model", "value": "qwen2.5-coder:7b"})["result"]
        assert any("'tools' capability" in text for text in result["_meta"]["warnings"])
    finally:
        w.close()


def test_the_model_list_extension_lists_ollama_models_and_reports_when_it_is_down(tmp_path, workspace, config_file):
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        result = _call(w, EXT_MODELS, {})["result"]
        assert result["models"] == ["qwen2.5-coder:7b", "qwen3-vl:8b"]
        assert result["error"] is None
    finally:
        w.close()
    client = FakeOllama([], list_error=ConnectionError("refused"))
    down = _wire(workspace, [], _store(tmp_path), config_file, client=client)
    try:
        initialize(down)
        result = _call(down, EXT_MODELS, {})["result"]
        assert result["models"] == []
        assert "Cannot reach Ollama" in result["error"]
    finally:
        down.close()


# ----- the settings editor -----------------------------------------------------------------------

def test_settings_round_trip_through_the_server(tmp_path, workspace, config_file):
    config_file.parent.mkdir(parents=True)
    config_file.write_text("# my settings\nnum_ctx = 8192  # keep the context\n", encoding="utf-8")
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        before = _call(w, EXT_SETTINGS_GET, {})["result"]
        assert before["values"]["num_ctx"] == 8192
        assert before["applies"] == "restart"
        assert before["exists"] is True

        saved = _call(w, EXT_SETTINGS_SET, {"values": {"num_ctx": 4096, "model": "qwen2.5-coder:7b",
                                                       "tool_profile": "minimal", "max_tool_rounds": 5}})
        assert saved["result"]["saved"] is True
        after = _call(w, EXT_SETTINGS_GET, {})["result"]
        assert after["values"]["num_ctx"] == 4096
        assert after["values"]["model"] == "qwen2.5-coder:7b"
        assert after["values"]["tool_profile"] == "minimal"
        assert after["values"]["max_tool_rounds"] == 5

        text = config_file.read_text(encoding="utf-8")
        assert "# my settings" in text and "# keep the context" in text
        # The next start reads the same file.
        restarted = config.load_settings(config_file=config_file, env={})
        assert (restarted.num_ctx, restarted.model, restarted.tool_profile) == (4096, "qwen2.5-coder:7b", "minimal")
    finally:
        w.close()


@pytest.mark.parametrize("values", [
    {"network_enabled": True},
    {"workspace": "C:\\"},
    {"plugin_dir": "C:\\evil"},
    {"plugins_enabled": False},
    {"mcp_servers": {"x": {"command": "calc"}}},
    {"test_command": "calc"},
    {"num_ctx": 10},
    {"model": ""},
])
def test_settings_set_refuses_keys_outside_the_allowlist_and_bad_values(tmp_path, workspace, config_file, values):
    config_file.parent.mkdir(parents=True)
    config_file.write_text("model = \"qwen3-vl:8b\"\n", encoding="utf-8")
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        response = _call(w, EXT_SETTINGS_SET, {"values": values})
        assert response["error"]["code"] == INVALID_PARAMS
        assert config_file.read_text(encoding="utf-8") == "model = \"qwen3-vl:8b\"\n"
    finally:
        w.close()


def test_settings_set_needs_an_object_of_values(tmp_path, workspace, config_file):
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        assert _call(w, EXT_SETTINGS_SET, {"values": ["model"]})["error"]["code"] == INVALID_PARAMS
    finally:
        w.close()


def test_settings_get_reports_a_broken_file_without_writing_it(tmp_path, workspace, config_file):
    config_file.parent.mkdir(parents=True)
    config_file.write_text("model = [oops\n", encoding="utf-8")
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        assert _call(w, EXT_SETTINGS_GET, {})["error"]["code"] == INVALID_PARAMS
        assert _call(w, EXT_SETTINGS_SET, {"values": {"num_ctx": 4096}})["error"]["code"] == INVALID_PARAMS
        assert config_file.read_text(encoding="utf-8") == "model = [oops\n"
    finally:
        w.close()


def test_settings_get_without_a_file_gives_the_defaults(tmp_path, workspace, config_file):
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        result = _call(w, EXT_SETTINGS_GET, {})["result"]
        assert result["exists"] is False
        assert result["values"]["num_ctx"] == Settings().num_ctx
    finally:
        w.close()


def test_the_extension_methods_need_initialize_first(tmp_path, workspace, config_file):
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        assert _call(w, EXT_SETTINGS_GET, {})["error"]["code"] == INVALID_REQUEST
        assert _call(w, "session/list", {})["error"]["code"] == INVALID_REQUEST
    finally:
        w.close()


def test_a_save_failure_is_a_warning_in_the_prompt_answer(tmp_path, workspace, config_file):
    w = _wire(workspace, [text_reply("answer")], _store(tmp_path), config_file)
    try:
        initialize(w)
        sid = new_session(w, workspace)

        def refuse(**kwargs):
            raise sessions.SessionError("the disk is full")

        w.server._store.save = refuse
        _, messages = prompt(w, sid, "hi")
        result = messages[-1]["result"]
        assert result["stopReason"] == "end_turn"
        assert any("was not saved" in text and "disk is full" in text for text in result["_meta"]["warnings"])
    finally:
        w.close()


def test_a_clean_save_adds_no_warning(tmp_path, workspace, config_file):
    w = _wire(workspace, [text_reply("answer")], _store(tmp_path), config_file)
    try:
        initialize(w)
        sid = new_session(w, workspace)
        _, messages = prompt(w, sid, "hi")
        assert "warnings" not in messages[-1]["result"].get("_meta", {})
    finally:
        w.close()


def test_a_host_change_is_logged_on_the_server(tmp_path, workspace, config_file):
    w = _wire(workspace, [], _store(tmp_path), config_file)
    try:
        initialize(w)
        response = _call(w, EXT_SETTINGS_SET, {"values": {"host": "http://127.0.0.1:9999"}})
        assert response["result"]["values"]["host"] == "http://127.0.0.1:9999"
        assert any("Ollama address" in line and "127.0.0.1:9999" in line for line in w.log)
        refused = _call(w, EXT_SETTINGS_SET, {"values": {"host": "http://example.com:11434"}})
        assert refused["error"]["code"] == INVALID_PARAMS
        assert "by hand" in refused["error"]["message"]
    finally:
        w.close()
