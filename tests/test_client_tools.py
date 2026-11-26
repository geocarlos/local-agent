"""The client's file and terminal methods (T20), driven in-process like test_server.py.

When the client advertises fs.readTextFile, fs.writeTextFile or terminal in initialize, the core's read_file,
write_file, edit_file, run_command and run_tests send fs/* and terminal/* requests to the client and use the
answers. The test plays the editor: `editor(method, params)` answers each request, raises Fail for an error
answer, or returns SILENT for no answer. Approvals stay on the core side and are answered as in test_server.py.
"""

import json
from pathlib import Path

from fakes import text_reply, tool_reply
from local_agent.client_tools import NOT_FOUND
from local_agent.config import Settings
from local_agent.frontends import server as server_module
from test_server import Wire, assert_tool_calls_answered, cancel_now, new_session, updates

ALL_CAPS = {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True}
SILENT = object()


class Fail(Exception):
    """An error answer from the editor."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def init_with(w: Wire, caps) -> dict:
    params = {"protocolVersion": 1} if caps is None else {"protocolVersion": 1, "clientCapabilities": caps}
    return w.read_until(w.request("initialize", params))[-1]


def run(w: Wire, session_id: str, text: str, editor, *, answer=None) -> list[dict]:
    """A prompt. Each fs/* or terminal/* request goes to `editor`; permission requests get answer(request)."""
    request_id = w.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]})
    messages: list[dict] = []
    while True:
        message = w.read()
        messages.append(message)
        method = message.get("method", "")
        if method == "session/request_permission":
            w.respond_permission(message["id"], answer(message) if answer is not None else "allow_once")
        elif method.startswith(("fs/", "terminal/")):
            try:
                result = editor(method, message["params"])
            except Fail as e:
                w.send(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                   "error": {"code": e.code, "message": e.message}}) + "\n")
                continue
            if result is not SILENT:
                w.send(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        elif message.get("id") == request_id and "method" not in message:
            return messages


def tool_message(w: Wire) -> str:
    """The content of the last tool message the model was sent (the answer to its last tool call)."""
    return [m for m in w.client.calls[-1]["messages"] if m["role"] == "tool"][-1]["content"]


def recorder(answers: dict):
    """An editor that records each request and answers from `answers` (a dict, or a callable(params))."""
    calls: list[tuple[str, dict]] = []

    def editor(method: str, params: dict):
        calls.append((method, params))
        answer = answers.get(method)  # a method not listed gets null, the answer a write gets
        if callable(answer):
            return answer(params)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return editor, calls


def methods(calls: list[tuple[str, dict]]) -> list[str]:
    return [method for method, _ in calls]


# ----- the capability decides who does the work -------------------------------------------------------


def test_without_the_capability_the_tools_use_the_disk(workspace):
    (workspace / "a.txt").write_text("disk text\n", encoding="utf-8")
    w = Wire(workspace, [tool_reply(("read_file", {"path": "a.txt"})), text_reply("ok")])
    init_with(w, None)
    session_id = new_session(w, workspace)
    editor, calls = recorder({})
    run(w, session_id, "read", editor)

    assert calls == []  # no client request at all
    assert "disk text" in tool_message(w)
    w.close()


def test_a_write_without_the_capability_goes_to_the_disk(workspace):
    w = Wire(workspace, [tool_reply(("write_file", {"path": "out.txt", "content": "local"})), text_reply("ok")])
    init_with(w, {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False})
    session_id = new_session(w, workspace)
    editor, calls = recorder({})
    run(w, session_id, "write", editor)

    assert calls == []
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "local"
    w.close()


def test_each_capability_is_separate(workspace):
    """A client that can read but not write: reads go to the editor, writes stay on the disk."""
    (workspace / "a.txt").write_text("disk\n", encoding="utf-8")
    w = Wire(workspace, [
        tool_reply(("read_file", {"path": "a.txt"}), ("write_file", {"path": "b.txt", "content": "written"})),
        text_reply("ok"),
    ])
    init_with(w, {"fs": {"readTextFile": True}})
    session_id = new_session(w, workspace)
    editor, calls = recorder({"fs/read_text_file": {"content": "from the editor\n"}})
    run(w, session_id, "both", editor)

    # The write's approval preview reads the editor's text for its diff; the write itself stays on the disk.
    assert methods(calls) == ["fs/read_text_file", "fs/read_text_file"]
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "written"
    w.close()


# ----- the file methods -------------------------------------------------------------------------------


def test_read_file_returns_the_editors_unsaved_text(workspace):
    (workspace / "a.txt").write_text("disk text\n", encoding="utf-8")
    w = Wire(workspace, [tool_reply(("read_file", {"path": "a.txt"})), text_reply("ok")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    editor, calls = recorder({"fs/read_text_file": {"content": "unsaved text\n"}})
    run(w, session_id, "read", editor)

    assert methods(calls) == ["fs/read_text_file"]
    params = calls[0][1]
    assert params["sessionId"] == session_id
    assert Path(params["path"]).resolve() == (workspace / "a.txt").resolve()  # the resolved, absolute path
    assert "unsaved text" in tool_message(w) and "disk text" not in tool_message(w)
    w.close()


def test_write_file_is_sent_to_the_editor_and_not_written_to_the_disk(workspace):
    w = Wire(workspace, [tool_reply(("write_file", {"path": "out.txt", "content": "hello"})), text_reply("saved")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    editor, calls = recorder({"fs/write_text_file": None})
    messages = run(w, session_id, "write", editor)

    # The approval preview asks the editor whether the file exists first (it does not, so no diff); then the write.
    assert methods(calls) == ["fs/read_text_file", "fs/write_text_file"]
    write = calls[-1][1]
    assert write["content"] == "hello"
    assert not (workspace / "out.txt").exists()
    assert tool_message(w) == f"Successfully wrote to {write['path']}."
    completed = [u for u in updates(messages) if u.get("status") == "completed"]
    assert len(completed) == 1
    w.close()


def test_a_write_the_editor_did_not_save_is_said_so_to_the_model(workspace):
    w = Wire(workspace, [tool_reply(("write_file", {"path": "out.txt", "content": "hello"})), text_reply("ok")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    editor, _ = recorder({"fs/write_text_file": {"_meta": {"saved": False}}})
    run(w, session_id, "write", editor)

    assert "not saved to disk yet" in tool_message(w)
    assert "unsaved changes" in tool_message(w)
    w.close()


def test_edit_file_works_on_the_editors_text_and_shows_that_diff(workspace):
    (workspace / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    w = Wire(workspace, [
        tool_reply(("edit_file", {"path": "a.txt", "old_text": "two unsaved", "new_text": "TWO"})),
        text_reply("done"),
    ])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    asked: list[dict] = []

    def answer(request: dict) -> str:
        asked.append(request)
        return "allow_once"

    editor, calls = recorder({
        "fs/read_text_file": {"content": "one\ntwo unsaved\nthree\n"},
        "fs/write_text_file": None,
    })
    run(w, session_id, "edit", editor, answer=answer)

    # The approval shows the diff against the editor's text, not against the disk.
    preview = asked[0]["params"]["toolCall"]["_meta"]["preview"]
    assert preview["kind"] == "diff"
    assert "-two unsaved" in preview["text"] and "+TWO" in preview["text"]
    write = [params for method, params in calls if method == "fs/write_text_file"]
    assert write and write[0]["content"] == "one\nTWO\nthree\n"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"  # the disk is untouched
    assert tool_message(w).startswith("Successfully edited")
    w.close()


def test_an_edit_that_does_not_match_the_editor_text_is_refused_before_the_question(workspace):
    (workspace / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    w = Wire(workspace, [
        tool_reply(("edit_file", {"path": "a.txt", "old_text": "two", "new_text": "TWO"})),
        text_reply("ok"),
    ])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    asked: list[dict] = []

    def answer(request: dict) -> str:
        asked.append(request)
        return "allow_once"

    editor, calls = recorder({"fs/read_text_file": {"content": "nothing here\n"}})
    run(w, session_id, "edit", editor, answer=answer)

    assert asked == []  # the precheck ran on the editor text and refused the edit
    assert "was not found" in tool_message(w)
    assert "fs/write_text_file" not in methods(calls)
    w.close()


def test_a_client_error_becomes_the_tool_result_and_the_turn_goes_on(workspace):
    w = Wire(workspace, [tool_reply(("read_file", {"path": "missing.txt"})), text_reply("no such file")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    editor, _ = recorder({"fs/read_text_file": Fail(NOT_FOUND, "no such file: missing.txt")})
    messages = run(w, session_id, "read", editor)

    assert "no such file: missing.txt" in tool_message(w)
    assert tool_message(w).startswith("Error: ")
    # The tool ran and answered with text, so the model sees a completed call whose result says why it failed.
    assert [u["status"] for u in updates(messages) if u["sessionUpdate"] == "tool_call_update"] == ["in_progress", "completed"]
    assert messages[-1]["result"]["stopReason"] == "end_turn"
    assert_tool_calls_answered(w.server._sessions[session_id].messages)
    w.close()


def test_a_client_that_never_answers_times_out_and_a_late_answer_is_dropped(workspace, monkeypatch):
    monkeypatch.setattr(server_module, "CLIENT_FILE_TIMEOUT_S", 0.2)
    w = Wire(workspace, [tool_reply(("read_file", {"path": "a.txt"})), text_reply("ok")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    seen: list[str] = []

    def editor(method: str, params: dict):
        seen.append(method)
        return SILENT

    run(w, session_id, "read", editor)

    assert "did not answer fs/read_text_file within 0.2 s" in tool_message(w)
    assert seen == ["fs/read_text_file"]
    assert w.server._abandoned  # the request is remembered as given up on
    # The late answer arrives after the turn ended. It is dropped quietly, and nothing is logged about it.
    late_id = next(iter(w.server._abandoned))
    w.send(json.dumps({"jsonrpc": "2.0", "id": late_id, "result": {"content": "late"}}) + "\n")
    w.read_until(w.request("initialize", {"protocolVersion": 1, "clientCapabilities": ALL_CAPS}))  # a round trip
    assert not any("unknown id" in line for line in w.log)
    assert not w.server._abandoned
    w.close()


def test_a_cancel_while_the_editor_is_asked_ends_the_turn_as_cancelled(workspace):
    w = Wire(workspace, [tool_reply(("read_file", {"path": "a.txt"})), text_reply("never")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)

    def editor(method: str, params: dict):
        cancel_now(w, session_id)  # the user cancels while the editor has not answered
        return SILENT

    messages = run(w, session_id, "read", editor)

    response = messages[-1]
    assert response["result"]["stopReason"] == "cancelled"
    # No model call follows a cancel, so the answer to the call is in the history, not in a request.
    history = w.server._sessions[session_id].messages
    assert {"role": "tool", "content": "Cancelled by user.", "tool_name": "read_file"} in history
    assert_tool_calls_answered(history)
    w.close()


# ----- the terminal methods ---------------------------------------------------------------------------


def terminal_editor(*, exit_code=0, output="hi\n", truncated=False, silent_wait=False, timeout_wait=False):
    """An editor for one command: create gives t1, wait gives the exit code, output the text, the rest are empty."""
    def editor(method: str, params: dict):
        if method == "terminal/create":
            return {"terminalId": "t1"}
        if method == "terminal/wait_for_exit":
            return SILENT if (silent_wait or timeout_wait) else {"exitCode": exit_code}
        if method == "terminal/output":
            return {"output": output, "truncated": truncated,
                    "exitStatus": None if exit_code is None else {"exitCode": exit_code}}
        return None  # kill and release
    return editor


def test_run_command_runs_in_the_editors_terminal(workspace):
    w = Wire(workspace, [tool_reply(("run_command", {"command": "echo hi"})), text_reply("ok")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    calls: list[tuple[str, dict]] = []
    real = terminal_editor()

    def editor(method: str, params: dict):
        calls.append((method, params))
        return real(method, params)

    run(w, session_id, "run", editor)

    assert methods(calls) == ["terminal/create", "terminal/wait_for_exit", "terminal/output", "terminal/release"]
    create = calls[0][1]
    assert create["command"] == "echo hi" and create["args"] == [] and create["sessionId"] == session_id
    assert Path(create["cwd"]).resolve() == workspace.resolve()
    result = tool_message(w)
    assert result.startswith("EXIT CODE: 0")
    assert "hi" in result
    w.close()


def test_a_command_in_the_terminal_reports_its_exit_code_and_truncation(workspace):
    w = Wire(workspace, [tool_reply(("run_command", {"command": "false"})), text_reply("ok")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    run(w, session_id, "run", terminal_editor(exit_code=3, output="tail\n", truncated=True))

    assert tool_message(w).startswith("EXIT CODE: 3")
    assert "dropped the start of the output" in tool_message(w)
    w.close()


def test_a_command_over_its_time_limit_is_killed_in_the_editor(workspace):
    settings = Settings(workspace=workspace, command_timeout_s=1)
    w = Wire(workspace, [tool_reply(("run_command", {"command": "sleep 9"})), text_reply("ok")], settings=settings)
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    calls: list[tuple[str, dict]] = []
    real = terminal_editor(output="partial\n", timeout_wait=True)

    def editor(method: str, params: dict):
        calls.append((method, params))
        return real(method, params)

    run(w, session_id, "run", editor)

    assert "timed out after 1s and was killed" in tool_message(w)
    assert methods(calls) == ["terminal/create", "terminal/wait_for_exit", "terminal/kill", "terminal/output",
                              "terminal/release"]
    w.close()


def test_a_cancel_during_a_command_kills_it_and_releases_the_terminal(workspace):
    w = Wire(workspace, [tool_reply(("run_command", {"command": "sleep 9"})), text_reply("never")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    editor, calls = recorder({})
    real = terminal_editor(silent_wait=True)

    def cancelling(method: str, params: dict):
        calls.append((method, params))
        if method == "terminal/wait_for_exit":
            cancel_now(w, session_id)
        return real(method, params)

    messages = run(w, session_id, "run", cancelling)

    assert messages[-1]["result"]["stopReason"] == "cancelled"
    assert methods(calls) == ["terminal/create", "terminal/wait_for_exit", "terminal/kill", "terminal/release"]
    assert_tool_calls_answered(w.server._sessions[session_id].messages)
    w.close()


def test_a_terminal_command_that_the_editor_refuses_to_create_is_an_error_result(workspace):
    w = Wire(workspace, [tool_reply(("run_command", {"command": "echo"})), text_reply("ok")])
    init_with(w, ALL_CAPS)
    session_id = new_session(w, workspace)
    editor, _ = recorder({"terminal/create": Fail(-32603, "no shell")})
    run(w, session_id, "run", editor)

    assert tool_message(w).startswith("Error running command in the editor: no shell")
    w.close()


# ----- the sandbox and approvals stay in the core -----------------------------------------------------


def test_a_path_outside_the_workspace_still_asks_and_nothing_is_sent_when_refused(workspace):
    outside = workspace.parent / "outside-t20.txt"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        w = Wire(workspace, [tool_reply(("read_file", {"path": str(outside)})), text_reply("ok")])
        init_with(w, ALL_CAPS)
        session_id = new_session(w, workspace)
        asked: list[dict] = []

        def refuse(request: dict) -> str:
            asked.append(request)
            return "reject_once"

        editor, calls = recorder({"fs/read_text_file": {"content": "secret\n"}})
        run(w, session_id, "read", editor, answer=refuse)

        assert len(asked) == 1  # the outside question is asked, not skipped because the client can read
        assert calls == []
        assert "secret" not in tool_message(w)
        w.close()
    finally:
        outside.unlink(missing_ok=True)


def test_a_path_outside_the_workspace_that_is_allowed_is_read_through_the_editor(workspace):
    outside = workspace.parent / "outside-t20-allowed.txt"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        w = Wire(workspace, [tool_reply(("read_file", {"path": str(outside)})), text_reply("ok")])
        init_with(w, ALL_CAPS)
        session_id = new_session(w, workspace)
        editor, calls = recorder({"fs/read_text_file": {"content": "from the editor\n"}})
        run(w, session_id, "read", editor, answer=lambda r: "allow_once")

        assert methods(calls) == ["fs/read_text_file"]
        assert Path(calls[0][1]["path"]).resolve() == outside.resolve()
        assert "from the editor" in tool_message(w)
        w.close()
    finally:
        outside.unlink(missing_ok=True)

