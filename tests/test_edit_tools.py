"""edit_file: exact-match replace, match errors before any question, line endings, encodings, and the diff previews.

Tool functions are called directly for the rules. The loop-level tests check that a failed edit is
answered without an approval request, and that a good edit asks with a diff.
"""

import os

from fakes import text_reply, tool_reply

from wildcat_code_agent import tools
from wildcat_code_agent.config import Settings
from wildcat_code_agent.core import loop
from wildcat_code_agent.core.events import ApprovalRequest, Decision, ToolResult
from wildcat_code_agent.tools.edit_tools import MAX_PREVIEW_CHARS, unified_diff
from wildcat_code_agent.tools.registry import ToolContext


class Recording:
    """Approver that records each request and answers with one fixed decision."""

    def __init__(self, decision: Decision = Decision.ALLOW_ONCE):
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> Decision:
        self.requests.append(request)
        return self.decision


def edit(ws, **args) -> str:
    return tools.TOOLS["edit_file"].call(args, ToolContext(workspace=ws))


def precheck(ws, **args):
    return tools.TOOLS["edit_file"].precheck(args, ToolContext(workspace=ws))


def run_loop(client, ws, approver):
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "edit it"})
    loop.run_turn(messages, client=client, settings=Settings(workspace=ws), emit=events.append,
                  approver=approver)
    return messages, events


def result_of(events) -> ToolResult:
    return next(e for e in events if isinstance(e, ToolResult))


def write_bytes(path, data: bytes):
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------- one exact occurrence

def test_replaces_the_single_occurrence(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"alpha\nbeta\ngamma\n")

    result = edit(tmp_path, path=str(target), old_text="beta", new_text="BETA")

    assert result == f"Successfully edited {target}."
    assert target.read_bytes() == b"alpha\nBETA\ngamma\n"


def test_new_text_may_be_empty_to_delete(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"keep\ndrop\nkeep\n")

    edit(tmp_path, path=str(target), old_text="drop\n", new_text="")

    assert target.read_bytes() == b"keep\nkeep\n"


def test_utf8_text_is_kept_and_a_bom_is_not_touched(tmp_path):
    target = write_bytes(tmp_path / "a.txt", "\ufeffcafé\nnaïve\n".encode("utf-8"))

    edit(tmp_path, path=str(target), old_text="naïve", new_text="déjà")

    assert target.read_bytes() == "\ufeffcafé\ndéjà\n".encode("utf-8")


# ---------------------------------------------------------------- match errors: nothing is written

def test_zero_matches_is_an_error_and_changes_nothing(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"alpha\nbeta\n")

    result = edit(tmp_path, path=str(target), old_text="gamma", new_text="x")

    assert result.startswith("Error: old_text was not found in")
    assert "copy the lines exactly" in result
    assert "No change was made." in result
    assert target.read_bytes() == b"alpha\nbeta\n"


def test_a_whitespace_only_difference_is_named_but_not_applied(tmp_path):
    target = write_bytes(tmp_path / "a.py", b"def f():\n    return 1\n")

    result = edit(tmp_path, path=str(target), old_text="def f():\n  return 1", new_text="def g(): pass")

    assert "was not found" in result
    assert "starts at line 1" in result
    assert "copy that block exactly" in result
    assert target.read_bytes() == b"def f():\n    return 1\n"


def test_multiple_matches_name_the_count_and_the_lines(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"x\ny\nx\nz\nx\n")

    result = edit(tmp_path, path=str(target), old_text="x", new_text="w")

    assert result.startswith("Error: old_text matches 3 places in")
    assert "(at lines 1, 3, 5)" in result
    assert "exactly once" in result
    assert target.read_bytes() == b"x\ny\nx\nz\nx\n"


def test_overlapping_matches_count_as_ambiguous(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"aaa")

    result = edit(tmp_path, path=str(target), old_text="aa", new_text="b")

    assert "matches 2 places" in result
    assert "(at line 1)" in result  # both matches are on line 1: named once
    assert target.read_bytes() == b"aaa"


def test_a_long_list_of_matches_is_cut_in_the_message(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"x\n" * 8)

    result = edit(tmp_path, path=str(target), old_text="x", new_text="y")

    assert "matches 8 places" in result
    assert "(at lines 1, 2, 3, 4, 5 and 3 more)" in result


def test_empty_old_text_and_unchanged_text_are_errors(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"abc\n")

    assert "old_text is empty" in edit(tmp_path, path=str(target), old_text="", new_text="x")
    assert "change nothing" in edit(tmp_path, path=str(target), old_text="abc", new_text="abc")
    assert target.read_bytes() == b"abc\n"


def test_a_missing_file_is_an_error_that_points_to_write_file(tmp_path):
    result = edit(tmp_path, path=str(tmp_path / "nope.txt"), old_text="a", new_text="b")

    assert result.startswith("Error: no such file:")
    assert "use write_file" in result


def test_a_folder_is_an_error(tmp_path):
    result = edit(tmp_path, path=str(tmp_path), old_text="a", new_text="b")

    assert result.startswith("Error: cannot read")


# ---------------------------------------------------------------- line endings and encodings

def test_crlf_file_matches_lf_text_and_keeps_crlf(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"one\r\ntwo\r\nthree\r\n")

    edit(tmp_path, path=str(target), old_text="two\nthree", new_text="2\n3")

    assert target.read_bytes() == b"one\r\n2\r\n3\r\n"


def test_crlf_file_accepts_crlf_text_too(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"one\r\ntwo\r\n")

    edit(tmp_path, path=str(target), old_text="one\r\ntwo", new_text="1\r\n2")

    assert target.read_bytes() == b"1\r\n2\r\n"


def test_new_lines_in_a_crlf_file_get_crlf(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"one\r\ntwo\r\n")

    edit(tmp_path, path=str(target), old_text="one", new_text="1\nuno")

    assert target.read_bytes() == b"1\r\nuno\r\ntwo\r\n"


def test_lf_file_with_crlf_text_is_matched_and_stays_lf(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"a\nb\n")

    edit(tmp_path, path=str(target), old_text="a\r\n", new_text="A\r\n")

    assert target.read_bytes() == b"A\nb\n"


def test_mixed_line_endings_are_matched_as_written(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"a\r\nb\nc\n")

    edit(tmp_path, path=str(target), old_text="b\nc", new_text="B\nC")

    assert target.read_bytes() == b"a\r\nB\nC\n"


def test_a_crlf_file_diff_shows_the_matched_line_and_the_preview_never_writes(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"one\r\ntwo\r\n")

    request = tools.TOOLS["edit_file"].approval_request(
        "1.1", {"path": str(target), "old_text": "two", "new_text": "2"}, ToolContext(workspace=tmp_path))

    assert "-two" in request.preview.text and "+2" in request.preview.text
    assert target.read_bytes() == b"one\r\ntwo\r\n"


def test_a_binary_file_is_refused_cleanly(tmp_path):
    target = write_bytes(tmp_path / "a.bin", b"PK\x03\x04\x00binary\x00")

    result = edit(tmp_path, path=str(target), old_text="binary", new_text="text")

    assert result.startswith("Error:") and "binary" in result
    assert target.read_bytes() == b"PK\x03\x04\x00binary\x00"


def test_a_file_that_is_not_utf8_is_refused_cleanly(tmp_path):
    target = write_bytes(tmp_path / "latin.txt", "café\n".encode("latin-1"))

    result = edit(tmp_path, path=str(target), old_text="caf", new_text="x")

    assert "is not UTF-8 text" in result
    assert target.read_bytes() == "café\n".encode("latin-1")


# ---------------------------------------------------------------- through the loop: no question on an error

def test_a_failed_match_is_answered_without_asking(fake_ollama, tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"alpha\n")
    client = fake_ollama([
        tool_reply(("edit_file", {"path": str(target), "old_text": "beta", "new_text": "x"})),
        text_reply("I will look again"),
    ])
    approver = Recording()

    _, events = run_loop(client, tmp_path, approver)

    assert approver.requests == []
    assert not [e for e in events if isinstance(e, ApprovalRequest)]
    assert result_of(events).status == "failed"
    assert result_of(events).content.startswith("Error: old_text was not found")
    assert target.read_bytes() == b"alpha\n"


def test_an_ambiguous_match_is_answered_without_asking(fake_ollama, tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"x\nx\n")
    client = fake_ollama([
        tool_reply(("edit_file", {"path": str(target), "old_text": "x", "new_text": "y"})),
        text_reply("ok"),
    ])
    approver = Recording()

    _, events = run_loop(client, tmp_path, approver)

    assert approver.requests == []
    assert "matches 2 places" in result_of(events).content
    assert target.read_bytes() == b"x\nx\n"


def test_a_good_edit_asks_once_with_a_diff_and_offers_allow_always(fake_ollama, tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"alpha\nbeta\n")
    client = fake_ollama([
        tool_reply(("edit_file", {"path": str(target), "old_text": "beta", "new_text": "BETA"})),
        text_reply("done"),
    ])
    approver = Recording()

    _, events = run_loop(client, tmp_path, approver)

    assert len(approver.requests) == 1
    request = approver.requests[0]
    assert request.title == f"Edit '{target}' (replace one occurrence)"
    assert request.preview.kind == "diff"
    assert request.preview.path == str(target)
    assert "-beta" in request.preview.text and "+BETA" in request.preview.text
    assert request.options == ("allow_once", "allow_always", "reject_once")
    assert result_of(events).status == "completed"
    assert target.read_bytes() == b"alpha\nBETA\n"


def test_a_denied_edit_leaves_the_file_alone(fake_ollama, tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"alpha\n")
    client = fake_ollama([
        tool_reply(("edit_file", {"path": str(target), "old_text": "alpha", "new_text": "omega"})),
        text_reply("ok"),
    ])

    _, events = run_loop(client, tmp_path, Recording(Decision.REJECT_ONCE))

    assert result_of(events).content == "User denied file edit."
    assert target.read_bytes() == b"alpha\n"


def test_precheck_is_none_for_a_good_edit(tmp_path):
    target = write_bytes(tmp_path / "a.txt", b"alpha\n")

    assert precheck(tmp_path, path=str(target), old_text="alpha", new_text="omega") is None
    assert "not found" in precheck(tmp_path, path=str(target), old_text="zeta", new_text="omega")


# ---------------------------------------------------------------- diff previews

def test_the_diff_has_file_labels_and_three_lines_of_context():
    before = "".join(f"line{n}\n" for n in range(1, 11))
    after = before.replace("line5\n", "LINE5\n")

    diff = unified_diff(before, after, "C:/work/a.txt")

    assert "--- C:/work/a.txt (current)" in diff
    assert "+++ C:/work/a.txt (proposed)" in diff
    assert "@@ -2,7 +2,7 @@" in diff  # lines 2..8: three before and three after the change at line 5
    assert "-line5" in diff and "+LINE5" in diff


def test_a_huge_diff_is_truncated_with_a_marker():
    before = "".join(f"old {n}\n" for n in range(2000))
    after = "".join(f"new {n}\n" for n in range(2000))

    diff = unified_diff(before, after, "big.txt")

    assert len(diff) <= MAX_PREVIEW_CHARS + 60
    assert "more chars]" in diff


def test_a_last_line_without_a_newline_is_marked_and_not_given_one():
    diff = unified_diff("a\nb", "a\nB\n", "f.txt")

    assert "-b\n\\ No newline at end of file\n+B\n" in diff


def test_an_unchanged_text_has_a_no_changes_note():
    assert unified_diff("same\n", "same\n", "a.txt") == "(no changes)"


def test_write_file_on_an_existing_file_previews_a_diff(tmp_path):
    nl = os.linesep  # the file already has the platform's endings, so only the edited line differs
    target = write_bytes(tmp_path / "a.txt", f"old{nl}keep{nl}".encode("utf-8"))
    request = tools.TOOLS["write_file"].approval_request(
        "1.1", {"path": str(target), "content": "new\nkeep\n"}, ToolContext(workspace=tmp_path))

    assert request.title == f"Write 9 chars to '{target}'"
    assert request.preview.kind == "diff"
    assert request.preview.path == str(target)
    assert "-old" in request.preview.text and "+new" in request.preview.text
    assert " keep" in request.preview.text  # unchanged line shown as context


def test_write_file_on_a_new_file_previews_the_content(tmp_path):
    request = tools.TOOLS["write_file"].approval_request(
        "1.1", {"path": str(tmp_path / "new.txt"), "content": "hello\n"}, ToolContext(workspace=tmp_path))

    assert request.preview.kind == "text"
    assert request.preview.text == "hello\n"


def test_write_file_on_a_binary_file_falls_back_to_the_content(tmp_path):
    target = write_bytes(tmp_path / "a.bin", b"\x00\x01")
    request = tools.TOOLS["write_file"].approval_request(
        "1.1", {"path": str(target), "content": "text"}, ToolContext(workspace=tmp_path))

    assert request.preview.kind == "text"
    assert request.preview.text == "text"


def test_write_file_diff_matches_what_windows_writes_for_newlines(tmp_path, monkeypatch):
    # write_file writes text mode, so "\n" becomes os.linesep. The diff must show that, not a silent change.
    import os
    target = write_bytes(tmp_path / "a.txt", b"old\r\n")
    monkeypatch.setattr(os, "linesep", "\r\n")
    request = tools.TOOLS["write_file"].approval_request(
        "1.1", {"path": str(target), "content": "old\n"}, ToolContext(workspace=tmp_path))

    assert "-old\r\n" not in request.preview.text  # same ending on both sides: no change shown
    assert "(no changes)" in request.preview.text
