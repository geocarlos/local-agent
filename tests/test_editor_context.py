"""Editor context in a prompt (T21): embedded text and resource_link blocks become context in the user message.

The unit tests cover the parser and the builder (attachments.py). The server tests drive the in-process stdio
server (test_server.py's Wire) with the fake Ollama client; the editor (the client's fs/* and _local-agent/*
answers) is a callable that the test passes in.
"""

import json
from pathlib import Path

import pytest

from fakes import text_reply
from local_agent import attachments
from local_agent.attachments import Attachment, Prompt, build_message, clean_diagnostics, display_text, parse_prompt
from local_agent.client_tools import ClientError
from test_server import Wire, initialize, new_session, updates

EDITOR_CAPS = {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True}
DIAG_CAPS = {"_meta": {"local-agent": {"diagnostics": True}}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def link_block(uri: str, name: str = "f") -> dict:
    return {"type": "resource_link", "uri": uri, "name": name}


def embedded_block(uri: str, text: str) -> dict:
    return {"type": "resource", "resource": {"uri": uri, "text": text, "mimeType": "text/plain"}}


# ----- the parser ---------------------------------------------------------------------------------


def test_plain_text_blocks_parse_to_the_same_text_with_no_attachments():
    prompt = parse_prompt([text_block("  hello "), text_block("there")])
    assert prompt.text == "hello \nthere".strip()
    assert prompt.attachments == []


def test_a_line_range_fragment_is_split_from_the_uri():
    prompt = parse_prompt([embedded_block("file:///w/app.py#L10-L12", "x")])
    (item,) = prompt.attachments
    assert item.uri == "file:///w/app.py"
    assert item.lines == (10, 12)


def test_a_single_line_fragment_and_no_fragment():
    (single,) = parse_prompt([embedded_block("file:///w/a.py#L7", "x")]).attachments
    assert single.lines == (7, 7)
    (whole,) = parse_prompt([embedded_block("file:///w/a.py", "x")]).attachments
    assert whole.lines is None and whole.uri == "file:///w/a.py"


def test_a_resource_link_is_a_link_attachment():
    (item,) = parse_prompt([text_block("q"), link_block("file:///w/a.py")]).attachments
    assert item == Attachment(kind="link", uri="file:///w/a.py")


@pytest.mark.parametrize("blocks", [
    [{"type": "image", "data": "AAAA", "mimeType": "image/png"}],
    [{"type": "resource", "resource": {"uri": "file:///w/a.bin", "blob": "AAAA"}}],
    [{"type": "resource", "resource": {"uri": "file:///w/a.py"}}],
    [{"type": "resource_link"}],
    [text_block("   ")],
    [],
    "not a list",
])
def test_unusable_blocks_are_refused(blocks):
    with pytest.raises(attachments.PromptError):
        parse_prompt(blocks)


def test_a_lone_surrogate_in_embedded_text_is_refused():
    with pytest.raises(attachments.PromptError, match="not valid Unicode"):
        parse_prompt([embedded_block("file:///w/a.py", "\ud800")])


# ----- the builder --------------------------------------------------------------------------------


def test_without_attachments_the_message_is_the_text_unchanged(tmp_path):
    assert build_message(Prompt(text="hi"), workspace=tmp_path, num_ctx=8192) == "hi"


def test_the_budget_is_half_the_window_at_three_characters_per_token_and_capped():
    assert attachments.budget_chars(8192) == 12288
    assert attachments.budget_chars(10**9) == attachments.MAX_CONTEXT_CHARS
    assert attachments.budget_chars(0) == 0


def test_an_embedded_selection_is_a_delimited_section_with_its_lines(tmp_path):
    prompt = parse_prompt([text_block("what is this?"), embedded_block((tmp_path / "app.py").as_uri() + "#L2-L3",
                                                                       "y = 2\nz = 3")])
    message = build_message(prompt, workspace=tmp_path, num_ctx=8192)
    assert message.startswith("what is this?" + attachments.CONTEXT_MARK)
    assert "--- file: app.py (lines 2-3, the selected text) ---\ny = 2\nz = 3\n--- end of app.py ---" in message


def test_an_embedded_text_outside_the_workspace_is_included_as_sent(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere" / "buf.py"
    prompt = parse_prompt([text_block("q"), embedded_block(outside.as_uri(), "secret = 1")])
    message = build_message(prompt, workspace=workspace, num_ctx=8192)
    assert "secret = 1" in message
    assert str(outside) in message  # outside the workspace the label is the absolute path


def test_a_resource_link_inside_the_workspace_is_read_from_disk(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"disk text\n")
    prompt = parse_prompt([text_block("q"), link_block((tmp_path / "a.txt").as_uri())])
    message = build_message(prompt, workspace=tmp_path, num_ctx=8192)
    assert "--- file: a.txt (whole file) ---\ndisk text\n" in message


def test_a_resource_link_outside_the_workspace_is_not_read(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("do not leak", encoding="utf-8")
    prompt = parse_prompt([text_block("q"), link_block(outside.as_uri())])
    message = build_message(prompt, workspace=workspace, num_ctx=8192)
    assert "do not leak" not in message
    assert "not included" in message and "outside the workspace" in message


def test_a_resource_link_to_a_missing_file_or_a_non_file_uri_is_a_note(tmp_path):
    prompt = parse_prompt([text_block("q"), link_block((tmp_path / "nope.txt").as_uri()),
                           link_block("https://example.com/a.txt")])
    message = build_message(prompt, workspace=tmp_path, num_ctx=8192)
    assert "not included: nope.txt" in message
    assert "only files in the workspace" in message


def test_a_resource_link_is_read_through_the_editor_when_the_client_can_read(tmp_path):
    (tmp_path / "a.txt").write_text("on disk only", encoding="utf-8")
    asked: list[str] = []

    class Editor:
        can_read = True

        def read_text_file(self, path):
            asked.append(path)
            return "unsaved text"

    prompt = parse_prompt([text_block("q"), link_block((tmp_path / "a.txt").as_uri())])
    message = build_message(prompt, workspace=tmp_path, num_ctx=8192, client=Editor())
    assert "unsaved text" in message and "on disk only" not in message
    assert asked == [str(tmp_path / "a.txt")]


def test_an_editor_read_error_is_a_note_not_a_failure(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    class Editor:
        can_read = True

        def read_text_file(self, path):
            raise ClientError("the editor could not read it")

    prompt = parse_prompt([text_block("q"), link_block((tmp_path / "a.txt").as_uri())])
    message = build_message(prompt, workspace=tmp_path, num_ctx=8192, client=Editor())
    assert "the editor could not give the text" in message


def test_a_file_over_the_size_limit_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_LINK_FILE_BYTES", 4)
    (tmp_path / "big.txt").write_text("12345", encoding="utf-8")
    prompt = parse_prompt([text_block("q"), link_block((tmp_path / "big.txt").as_uri())])
    message = build_message(prompt, workspace=tmp_path, num_ctx=8192)
    assert "more than the 4 bytes" in message and "12345" not in message


def test_a_long_section_is_cut_and_the_model_is_told(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_ITEM_CHARS", 10)
    prompt = parse_prompt([text_block("q"), embedded_block((tmp_path / "a.py").as_uri(), "x" * 100)])
    message = build_message(prompt, workspace=tmp_path, num_ctx=8192)
    assert "[truncated: the first 10 of 100 characters are included]" in message
    assert "x" * 11 not in message


def test_the_whole_context_is_capped_by_the_window_and_later_items_are_noted(tmp_path):
    # num_ctx 512 gives a budget of 768 characters: the first item uses it all, the second is not included.
    prompt = parse_prompt([text_block("q"), embedded_block((tmp_path / "a.py").as_uri(), "a" * 1000),
                           embedded_block((tmp_path / "b.py").as_uri(), "b" * 10)])
    message = build_message(prompt, workspace=tmp_path, num_ctx=512)
    assert "[truncated: the first 768 of 1000 characters are included]" in message
    assert "not included: b.py: the context limit is reached" in message
    assert "b" * 10 not in message


def test_diagnostics_of_an_attached_file_follow_it(tmp_path):
    class Editor:
        can_read = False
        can_diagnostics = True

        def diagnostics(self, uris):
            assert uris == [(tmp_path / "a.py").as_uri()]
            return {(tmp_path / "a.py").as_uri(): [
                {"severity": "error", "startLine": 3, "startColumn": 5, "message": "boom", "source": "ts"},
            ]}

    prompt = parse_prompt([text_block("q"), embedded_block((tmp_path / "a.py").as_uri(), "x = 1")])
    message = build_message(prompt, workspace=tmp_path, num_ctx=8192, client=Editor())
    assert "--- diagnostics for file:" in message
    assert "line 3, column 5: error (ts): boom" in message


def test_a_diagnostics_failure_leaves_the_context_complete(tmp_path):
    class Editor:
        can_read = False
        can_diagnostics = True

        def diagnostics(self, uris):
            raise ClientError("no answer")

    prompt = parse_prompt([text_block("q"), embedded_block((tmp_path / "a.py").as_uri(), "x = 1")])
    message = build_message(prompt, workspace=tmp_path, num_ctx=8192, client=Editor())
    assert "x = 1" in message and "diagnostics" not in message


def test_clean_diagnostics_drops_malformed_items():
    raw = {"files": [
        {"uri": "file:///a", "items": [
            {"severity": "warning", "startLine": 1, "startColumn": 2, "message": "ok"},
            {"severity": "fatal", "startLine": 1, "startColumn": 2, "message": "bad severity"},
            {"severity": "error", "startLine": "1", "startColumn": 2, "message": "bad line"},
        ]},
        {"items": []},
        "junk",
    ]}
    assert clean_diagnostics(raw) == {"file:///a": [
        {"severity": "warning", "startLine": 1, "startColumn": 2, "message": "ok"}]}
    assert clean_diagnostics("nope") == {}


def test_display_text_hides_the_context_and_keeps_the_typed_text():
    content = "what is wrong?" + attachments.CONTEXT_MARK + "\nlots of code"
    assert display_text(content) == "what is wrong?\n\n[with attached context]"
    assert display_text("plain") == "plain"


# ----- the server (in-process, with the fake model) ---------------------------------------------------


class Editor:
    """Answers the client's requests: fs/read_text_file from `files` (uri path -> text), diagnostics from `diags`."""

    def __init__(self, files: dict[str, str] | None = None, diags: dict | None = None) -> None:
        self.files = files or {}
        self.diags = diags or {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, params: dict):
        self.calls.append((method, params))
        if method == "fs/read_text_file":
            return {"content": self.files[params["path"]]}
        if method == "_local-agent/diagnostics":
            return {"files": [{"uri": uri, "items": self.diags.get(uri, [])} for uri in params["uris"]]}
        raise AssertionError(f"unexpected client call {method}")


def send(w: Wire, session_id: str, blocks: list, editor: Editor | None = None) -> list[dict]:
    """A session/prompt with these content blocks. Client calls go to `editor`; permissions are allowed once."""
    request_id = w.request("session/prompt", {"sessionId": session_id, "prompt": blocks})
    messages: list[dict] = []
    while True:
        message = w.read()
        messages.append(message)
        method = message.get("method", "")
        if method == "session/request_permission":
            w.respond_permission(message["id"], "allow_once")
        elif method.startswith(("fs/", "_local-agent/")):
            result = editor(method, message["params"]) if editor is not None else None
            w.send(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        elif message.get("id") == request_id and "method" not in message:
            return messages


def init_caps(w: Wire, caps: dict | None) -> dict:
    params = {"protocolVersion": 1} if caps is None else {"protocolVersion": 1, "clientCapabilities": caps}
    return w.read_until(w.request("initialize", params))[-1]


def user_content(w: Wire) -> str:
    """The content of the last user message the model was sent."""
    return [m for m in w.client.calls[-1]["messages"] if m["role"] == "user"][-1]["content"]


def test_the_server_advertises_embedded_context(workspace):
    w = Wire(workspace, [])
    try:
        caps = initialize(w)["result"]["agentCapabilities"]["promptCapabilities"]
        assert caps == {"image": False, "audio": False, "embeddedContext": True}
    finally:
        w.close()


def test_plain_text_prompts_are_unchanged(workspace):
    w = Wire(workspace, [text_reply("hi")])
    try:
        initialize(w)
        sid = new_session(w, workspace)
        send(w, sid, [text_block("just text")])
        assert user_content(w) == "just text"
    finally:
        w.close()


def test_an_embedded_open_file_reaches_the_model_and_no_read_file_is_called(workspace):
    # The "Done when" in test form: the model answers from the attached file, and makes no tool call.
    w = Wire(workspace, [text_reply("It prints the greeting.")])
    try:
        initialize(w)
        sid = new_session(w, workspace)
        uri = (workspace / "greet.py").as_uri()
        messages = send(w, sid, [text_block("What does the open file do?"),
                                 embedded_block(uri, "print('hello from the open file')\n")])
        reply = [m for m in messages if m.get("method") == "session/update"]
        assert any("It prints the greeting." in m["params"]["update"].get("content", {}).get("text", "")
                   for m in reply)
        sent = w.client.calls[-1]["messages"]
        assert "print('hello from the open file')" in user_content(w)
        assert all(not m.get("tool_calls") for m in sent)  # the model was not offered a call it made
        assert not any(m["role"] == "tool" for m in sent)
        assert len(w.client.calls) == 1  # one model request: the answer came without a tool round
    finally:
        w.close()


def test_a_resource_link_is_read_by_the_server_and_sent(workspace):
    (workspace / "notes.md").write_bytes(b"alpha beta\n")
    w = Wire(workspace, [text_reply("ok")])
    try:
        initialize(w)
        sid = new_session(w, workspace)
        send(w, sid, [text_block("summarise"), link_block((workspace / "notes.md").as_uri(), "notes.md")])
        assert "--- file: notes.md (whole file) ---\nalpha beta\n" in user_content(w)
    finally:
        w.close()


def test_a_resource_link_outside_the_workspace_is_refused_without_a_question(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("TOP SECRET", encoding="utf-8")
    w = Wire(workspace, [text_reply("I did not see it.")])
    try:
        initialize(w)
        sid = new_session(w, workspace)
        messages = send(w, sid, [text_block("read it"), link_block(outside.as_uri(), "secret.txt")])
        assert not any(m.get("method") == "session/request_permission" for m in messages)
        assert "TOP SECRET" not in user_content(w)
        assert "outside the workspace" in user_content(w)
    finally:
        w.close()


def test_a_resource_link_read_through_the_editor_sends_the_unsaved_text(workspace):
    (workspace / "dirty.py").write_bytes(b"on disk only\n")
    editor = Editor(files={str(workspace / "dirty.py"): "unsaved\n"})
    w = Wire(workspace, [text_reply("ok")])
    try:
        init_caps(w, EDITOR_CAPS)
        sid = new_session(w, workspace)
        send(w, sid, [text_block("q"), link_block((workspace / "dirty.py").as_uri(), "dirty.py")], editor)
        assert "unsaved\n" in user_content(w) and "on disk only" not in user_content(w)
        assert editor.calls == [("fs/read_text_file", {"sessionId": sid, "path": str(workspace / "dirty.py")})]
    finally:
        w.close()


def test_a_blob_resource_is_invalid_params_and_the_server_goes_on(workspace):
    w = Wire(workspace, [text_reply("ok")])
    try:
        initialize(w)
        sid = new_session(w, workspace)
        request_id = w.request("session/prompt", {"sessionId": sid, "prompt": [
            {"type": "resource", "resource": {"uri": "file:///x.bin", "blob": "AAAA"}}]})
        response = w.read_until(request_id)[-1]
        assert response["error"]["code"] == -32602
        send(w, sid, [text_block("still here")])
        assert user_content(w) == "still here"
    finally:
        w.close()


def test_the_context_is_limited_by_the_window(workspace):
    from local_agent.config import Settings
    w = Wire(workspace, [text_reply("ok")], settings=Settings(workspace=workspace, num_ctx=512))
    try:
        initialize(w)
        sid = new_session(w, workspace)
        send(w, sid, [text_block("q"), embedded_block((workspace / "big.py").as_uri(), "z" * 5000)])
        content = user_content(w)
        assert "[truncated: the first 768 of 5000 characters are included]" in content
    finally:
        w.close()


def test_diagnostics_of_the_attached_file_come_from_the_editor(workspace):
    uri = (workspace / "app.py").as_uri()
    editor = Editor(diags={uri: [{"severity": "error", "startLine": 4, "startColumn": 1, "message": "undefined x",
                                  "source": "pyright"}]})
    w = Wire(workspace, [text_reply("ok")])
    try:
        init_caps(w, {**EDITOR_CAPS, **DIAG_CAPS})
        sid = new_session(w, workspace)
        send(w, sid, [text_block("why?"), embedded_block(uri, "x\n")], editor)
        assert ("_local-agent/diagnostics", {"sessionId": sid, "uris": [uri]}) in editor.calls
        assert "line 4, column 1: error (pyright): undefined x" in user_content(w)
    finally:
        w.close()


def test_diagnostics_are_not_asked_when_the_client_does_not_declare_them(workspace):
    editor = Editor()
    w = Wire(workspace, [text_reply("ok")])
    try:
        init_caps(w, EDITOR_CAPS)
        sid = new_session(w, workspace)
        send(w, sid, [text_block("q"), embedded_block((workspace / "a.py").as_uri(), "x")], editor)
        assert editor.calls == []
    finally:
        w.close()


def test_the_history_keeps_the_context_and_a_replay_shows_only_the_typed_text(workspace):
    w = Wire(workspace, [text_reply("answer")])
    try:
        initialize(w)
        sid = new_session(w, workspace)
        send(w, sid, [text_block("look"), embedded_block((workspace / "a.py").as_uri(), "marker_text = 1")])
        saved = w.server._sessions[sid].messages
        assert "marker_text = 1" in [m for m in saved if m["role"] == "user"][-1]["content"]
        request_id = w.request("session/load", {"sessionId": sid})
        replay = w.read_until(request_id)
        shown = [u["content"]["text"] for u in updates(replay) if u["sessionUpdate"] == "user_message_chunk"]
        assert shown == ["look\n\n[with attached context]"]
    finally:
        w.close()


def test_an_empty_context_only_prompt_is_a_text_less_prompt_with_attachments(workspace):
    # A prompt with only an attached file (no typed text) is valid: the context is the message.
    w = Wire(workspace, [text_reply("ok")])
    try:
        initialize(w)
        sid = new_session(w, workspace)
        send(w, sid, [embedded_block((workspace / "a.py").as_uri(), "only_context = 1")])
        assert "only_context = 1" in user_content(w)
    finally:
        w.close()
