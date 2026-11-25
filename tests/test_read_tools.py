"""Read-tier tools: read_file line ranges, list_dir, glob, grep, git_status, git_diff, git_log.

Each tool is tested directly against a temp workspace (tool.call with a ToolContext). The
git tools use a temp repository created in the test. The loop-level tests check that the
tools are auto-allowed and that a path outside the workspace goes through approval.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fakes import text_reply, tool_reply

from local_agent import tools
from local_agent.config import Settings
from local_agent.core import loop
from local_agent.core.events import ApprovalRequest, Decision, ToolResult
from local_agent.tools import read_tools
from local_agent.tools.registry import ToolContext

NT = os.name == "nt"
SECRET = "outside secret"
READ_TOOLS = ("read_file", "list_dir", "glob", "grep", "git_status", "git_diff", "git_log")
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# ---------------------------------------------------------------- helpers and fixtures

@pytest.fixture
def ws(tmp_path, monkeypatch):
    """The workspace folder. The process cwd is another folder, so relative paths must not use it."""
    folder = tmp_path / "work"
    folder.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "elsewhere.txt").write_text("not in the workspace", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    return folder


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text(SECRET, encoding="utf-8")
    return folder


def call(name: str, ws: Path, max_output_chars: int = 8000, **args) -> str:
    """Run a read tool directly, with the same context the loop would give it."""
    context = ToolContext(workspace=ws, max_output_chars=max_output_chars)
    return tools.TOOLS[name].call(args, context)


def norm(text: str) -> str:
    """Forward slashes, so expected output is the same on Windows and elsewhere."""
    return text.replace("\\", "/")


def make_junction(link: Path, target: Path) -> None:
    if not NT:
        pytest.skip("junctions are a Windows feature")
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip(f"cannot create a junction here: {(done.stdout + done.stderr).strip()}")


@pytest.fixture
def repo(ws):
    """A git repository in the workspace with one commit: a.txt committed, b.txt untracked."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    def git(*args):
        subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    (ws / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "first commit")
    return ws


# ---------------------------------------------------------------- read_file

@pytest.fixture
def ten_lines(ws):
    path = ws / "ten.txt"
    path.write_text("".join(f"line{n}\n" for n in range(1, 11)), encoding="utf-8")
    return path


def test_read_file_without_a_range_returns_the_whole_file(ws, ten_lines):
    assert call("read_file", ws, path=str(ten_lines)) == "".join(f"line{n}\n" for n in range(1, 11))


def test_read_file_line_range_is_numbered_with_a_header(ws, ten_lines):
    result = call("read_file", ws, path=str(ten_lines), start_line=3, end_line=5)

    assert result == "[lines 3-5 of 10]\n3: line3\n4: line4\n5: line5"


def test_read_file_start_only_reads_to_the_end(ws, ten_lines):
    result = call("read_file", ws, path=str(ten_lines), start_line=9)

    assert result == "[lines 9-10 of 10]\n9: line9\n10: line10"


def test_read_file_end_past_the_file_is_clipped(ws, ten_lines):
    result = call("read_file", ws, path=str(ten_lines), start_line=1, end_line=500)

    assert result.startswith("[lines 1-10 of 10]\n1: line1\n")


def test_read_file_bad_ranges_are_clean_errors(ws, ten_lines):
    assert call("read_file", ws, path=str(ten_lines), start_line=0).startswith("Error: bad line range")
    assert call("read_file", ws, path=str(ten_lines), start_line=5, end_line=4).startswith("Error: bad line range")
    assert call("read_file", ws, path=str(ten_lines), start_line=11).startswith(
        "Error: start_line 11 is past the end of the file (10 lines)")


def test_read_file_range_of_an_empty_file(ws):
    empty = ws / "empty.txt"
    empty.write_text("", encoding="utf-8")

    assert call("read_file", ws, path=str(empty), start_line=1) == "[lines 0-0 of 0: the file is empty]"


def test_read_file_missing_file_is_an_error_text(ws):
    assert call("read_file", ws, path=str(ws / "missing.txt")).startswith("Error: ")


def test_read_file_ranged_output_is_truncated_with_a_marker(ws, ten_lines):
    result = call("read_file", ws, max_output_chars=30, path=str(ten_lines), start_line=1, end_line=10)

    assert result.startswith("[lines 1-10 of 10]\n1: line1")
    assert "...[truncated " in result


def test_read_file_ranged_lines_split_only_on_newline(ws):
    # \x0b, \x0c, \x1c-\x1e, \x85, \u2028 and \u2029 are line breaks for str.splitlines() but not here.
    path = ws / "odd.txt"
    path.write_text("a\x0bb\nc\x0cd\ne\x1cf\x85g\u2028h\nlast\n", encoding="utf-8")

    result = call("read_file", ws, path=str(path), start_line=1, end_line=3)

    assert result == "[lines 1-3 of 4]\n1: a\x0bb\n2: c\x0cd\n3: e\x1cf\x85g\u2028h"


@pytest.mark.parametrize("content, total", [("a\nb\n", 2), ("a\nb", 2), ("a\r\nb\r\n", 2), ("\n", 1)])
def test_read_file_ranged_total_ignores_a_trailing_newline(ws, content, total):
    path = ws / "t.txt"
    path.write_text(content, encoding="utf-8", newline="")

    result = call("read_file", ws, path=str(path), start_line=1, end_line=99)

    assert result.startswith(f"[lines 1-{total} of {total}]\n1: ")
    assert f"{total + 1}:" not in result


# ---------------------------------------------------------------- list_dir

def test_list_dir_is_sorted_and_marks_folders(ws):
    (ws / "b.txt").write_text("", encoding="utf-8")
    (ws / "A.txt").write_text("", encoding="utf-8")
    (ws / "sub").mkdir()

    assert call("list_dir", ws, path=str(ws)) == "A.txt\nb.txt\nsub/"


def test_list_dir_without_a_path_lists_the_workspace_not_the_process_cwd(ws):
    (ws / "inside.txt").write_text("", encoding="utf-8")

    assert call("list_dir", ws) == "inside.txt"


def test_list_dir_errors_and_empty_folder(ws):
    (ws / "file.txt").write_text("", encoding="utf-8")
    (ws / "empty").mkdir()

    assert call("list_dir", ws, path=str(ws / "file.txt")).startswith("Error: not a folder")
    assert call("list_dir", ws, path=str(ws / "empty")) == "(empty folder)"


def test_list_dir_caps_the_number_of_entries(ws, monkeypatch):
    monkeypatch.setattr(read_tools, "MAX_LIST_ENTRIES", 2)
    for name in ("a", "b", "c"):
        (ws / name).write_text("", encoding="utf-8")

    result = call("list_dir", ws)

    assert result == "a\nb\n[... 1 more entries not shown]"


# ---------------------------------------------------------------- glob

@pytest.fixture
def tree(ws):
    (ws / "src" / "pkg").mkdir(parents=True)
    (ws / "src" / "main.py").write_text("", encoding="utf-8")
    (ws / "src" / "pkg" / "util.py").write_text("", encoding="utf-8")
    (ws / "notes.md").write_text("", encoding="utf-8")
    (ws / ".git").mkdir()
    (ws / ".git" / "config.py").write_text("", encoding="utf-8")
    return ws


def test_glob_returns_workspace_relative_sorted_paths_with_folders_marked(tree):
    result = norm(call("glob", tree, pattern="*"))

    assert result == "notes.md\nsrc/"


def test_glob_recursive_pattern_skips_dot_git(tree):
    result = norm(call("glob", tree, pattern="**/*.py"))

    assert result == "src/main.py\nsrc/pkg/util.py"


def test_glob_base_path_restricts_the_search(tree):
    result = norm(call("glob", tree, pattern="**/*.py", path=str(tree / "src" / "pkg")))

    assert result == "src/pkg/util.py"


def test_glob_no_matches(tree):
    assert call("glob", tree, pattern="*.rs") == "No matches."


@pytest.mark.parametrize("pattern", ["../*", "src/../../*", "/etc/*", "\\\\server\\share\\*", "C:*", "a:b", "", "a\x00b"])
def test_glob_rejects_patterns_that_leave_the_base(tree, pattern):
    assert call("glob", tree, pattern=pattern).startswith("Error: the pattern must be relative")


def test_glob_caps_the_results(tree, monkeypatch):
    monkeypatch.setattr(read_tools, "MAX_GLOB_RESULTS", 2)
    for name in ("a1", "a2", "a3"):
        (tree / name).write_text("", encoding="utf-8")

    result = call("glob", tree, pattern="a*")

    assert result.splitlines()[:2] == ["a1", "a2"]
    assert result.splitlines()[2].startswith("[stopped at 2 results")


def test_glob_base_folder_that_is_not_a_folder_is_an_error(tree):
    assert call("glob", tree, pattern="*", path=str(tree / "notes.md")).startswith("Error: not a folder")


def test_glob_does_not_list_anything_through_a_junction_out_of_the_workspace(tree, outside):
    make_junction(tree / "link", outside)

    result = norm(call("glob", tree, pattern="**/*.txt"))

    assert "secret.txt" not in result
    assert result == "No matches."


# ---------------------------------------------------------------- grep

@pytest.fixture
def code(ws):
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("def main():\n    print('Hello')\n", encoding="utf-8")
    (ws / "src" / "lib.txt").write_text("hello again\nnothing here\n", encoding="utf-8")
    return ws


def test_grep_output_is_relpath_line_and_text(code):
    result = norm(call("grep", code, pattern="def main"))

    assert result == "src/app.py:1: def main():"


def test_grep_include_filters_by_file_name(code):
    result = norm(call("grep", code, pattern="hello", include="*.txt"))

    assert result == "src/lib.txt:1: hello again"


def test_grep_ignore_case(code):
    assert norm(call("grep", code, pattern="HELLO", ignore_case=True)) == (
        "src/app.py:2:     print('Hello')\nsrc/lib.txt:1: hello again")
    assert call("grep", code, pattern="HELLO") == "No matches."


def test_grep_invalid_regex_is_a_clean_error(code):
    result = call("grep", code, pattern="(unclosed")

    assert result.startswith("Error: invalid regular expression '(unclosed'")


def test_grep_skips_binary_files_and_dot_git(code):
    (code / "data.bin").write_bytes(b"needle\x00\x01needle")
    (code / ".git").mkdir()
    (code / ".git" / "HEAD").write_text("needle", encoding="utf-8")

    assert call("grep", code, pattern="needle") == "No matches."


def test_grep_single_file_path(code):
    result = norm(call("grep", code, pattern="nothing", path=str(code / "src" / "lib.txt")))

    assert result == "src/lib.txt:2: nothing here"


def test_grep_caps_the_matches_and_says_so(code, monkeypatch):
    monkeypatch.setattr(read_tools, "MAX_GREP_MATCHES", 2)
    (code / "many.txt").write_text("x\nx\nx\n", encoding="utf-8")

    lines = call("grep", code, pattern="x", include="many.txt").splitlines()

    assert lines[:2] == ["many.txt:1: x", "many.txt:2: x"]
    assert lines[2].startswith("[stopped at 2 matches")


def test_grep_does_not_search_through_a_junction_out_of_the_workspace(code, outside):
    make_junction(code / "link", outside)

    assert call("grep", code, pattern=SECRET) == "No matches."


def test_grep_result_is_truncated_with_a_marker(code):
    (code / "long.txt").write_text("match " * 100 + "\n", encoding="utf-8")

    result = call("grep", code, pattern="match", include="long.txt", max_output_chars=50)

    assert "...[truncated " in result


# ---------------------------------------------------------------- git (temp repository)

@requires_git
def test_git_status_shows_branch_untracked_and_modified(repo):
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (repo / "b.txt").write_text("new\n", encoding="utf-8")

    result = call("git_status", repo)

    assert result.splitlines()[0].startswith("## ")
    assert " M a.txt" in result
    assert "?? b.txt" in result


@requires_git
def test_git_diff_unstaged_staged_and_path_limited(repo):
    (repo / "a.txt").write_text("one\nTWO\n", encoding="utf-8")
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    subprocess.run(["git", "add", "c.txt"], cwd=repo, check=True, capture_output=True)

    unstaged = call("git_diff", repo)
    staged = call("git_diff", repo, staged=True)
    only_c = call("git_diff", repo, path=str(repo / "c.txt"), staged=True)

    assert "+TWO" in unstaged and "c.txt" not in unstaged
    assert "+c" in staged and "a.txt" not in staged
    assert "+c" in only_c and "a.txt" not in only_c


@requires_git
def test_git_diff_with_no_changes(repo):
    assert call("git_diff", repo) == "No differences."


@requires_git
def test_git_log_default_and_explicit_count(repo):
    for n in range(2, 6):
        (repo / "a.txt").write_text(f"v{n}\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-q", "-am", f"commit {n}"], cwd=repo, check=True, capture_output=True)

    assert len(call("git_log", repo).splitlines()) == 5
    two = call("git_log", repo, max_count=2).splitlines()
    assert len(two) == 2
    assert "commit 5" in two[0]


@requires_git
def test_git_log_rejects_a_bad_count(repo):
    assert call("git_log", repo, max_count=0).startswith("Error: max_count must be between 1 and")
    assert call("git_log", repo, max_count=10_000).startswith("Error: max_count must be between 1 and")


@requires_git
def test_git_tools_outside_a_repository_say_so(ws):
    assert call("git_status", ws).startswith("The workspace is not a git repository")
    assert call("git_diff", ws).startswith("The workspace is not a git repository")
    assert call("git_log", ws).startswith("The workspace is not a git repository")


def test_git_tools_report_a_missing_git(ws, monkeypatch):
    monkeypatch.setattr(read_tools.shutil, "which", lambda name: None)

    assert call("git_status", ws) == "Error: git is not installed or not on PATH."
    assert call("git_log", ws) == "Error: git is not installed or not on PATH."


@requires_git
def test_git_output_is_truncated_with_a_marker(repo):
    (repo / "a.txt").write_text("x" * 500 + "\n", encoding="utf-8")

    result = call("git_diff", repo, max_output_chars=60)

    assert "...[truncated " in result


# ---------------------------------------------------------------- through the loop: approvals and sandbox

class Scripted:
    """Approver that answers from a list of decisions (default: refuse) and records each request."""

    def __init__(self, *decisions: Decision):
        self.decisions = list(decisions)
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> Decision:
        self.requests.append(request)
        return self.decisions.pop(0) if self.decisions else Decision.REJECT_ONCE


def run(client, ws: Path, approver=None):
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "go"})
    loop.run_turn(messages, client=client, settings=Settings(workspace=ws), emit=events.append,
                  approver=approver)
    return messages, events


def results(events) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


@requires_git
def test_every_read_tool_runs_without_asking(fake_ollama, repo):
    (repo / "x.txt").write_text("hello\n", encoding="utf-8")
    calls = [
        ("read_file", {"path": "x.txt"}),
        ("list_dir", {}),
        ("glob", {"pattern": "*.txt"}),
        ("grep", {"pattern": "hello"}),
        ("git_status", {}),
        ("git_diff", {"path": "x.txt"}),
        ("git_log", {"max_count": 1}),
    ]
    client = fake_ollama([tool_reply(*calls), text_reply("done")])
    approver = Scripted()

    _, events = run(client, repo, approver)

    assert approver.requests == []
    assert not [e for e in events if isinstance(e, ApprovalRequest)]
    assert [r.name for r in results(events)] == [name for name, _ in calls]
    assert all(r.status == "completed" for r in results(events))


def test_read_tools_are_read_risk_and_declare_their_paths():
    for name in READ_TOOLS:
        assert tools.TOOLS[name].risk.value == "read", name
    assert tools.TOOLS["list_dir"].path_args == ("path",)
    assert tools.TOOLS["glob"].path_args == ("path",)
    assert tools.TOOLS["grep"].path_args == ("path",)
    assert tools.TOOLS["git_diff"].path_args == ("path",)
    assert tools.TOOLS["git_status"].path_args == ()
    assert tools.TOOLS["git_log"].path_args == ()


def test_an_omitted_path_means_the_workspace_even_when_the_cwd_differs(fake_ollama, ws):
    (ws / "inside.txt").write_text("", encoding="utf-8")
    client = fake_ollama([tool_reply(("list_dir", {}), ("glob", {"pattern": "*"})), text_reply("done")])
    approver = Scripted()

    _, events = run(client, ws, approver)

    assert approver.requests == []
    assert [r.content for r in results(events)] == ["inside.txt", "inside.txt"]


def test_a_null_optional_path_means_the_workspace(fake_ollama, ws):
    (ws / "inside.txt").write_text("", encoding="utf-8")
    client = fake_ollama([tool_reply(("list_dir", {"path": None})), text_reply("done")])

    _, events = run(client, ws, approver=None)

    assert results(events)[0].content == "inside.txt"
    assert results(events)[0].status == "completed"


@pytest.mark.parametrize(("name", "args"), [
    ("read_file", lambda out: {"path": str(out / "secret.txt")}),
    ("list_dir", lambda out: {"path": str(out)}),
    ("glob", lambda out: {"pattern": "*.txt", "path": str(out)}),
    ("grep", lambda out: {"pattern": "secret", "path": str(out)}),
    ("git_diff", lambda out: {"path": str(out / "secret.txt")}),
])
def test_a_path_outside_the_workspace_asks_and_is_denied_without_an_approver(fake_ollama, ws, outside, name, args):
    client = fake_ollama([tool_reply((name, args(outside))), text_reply("ok")])

    _, events = run(client, ws, approver=None)

    assert SECRET not in results(events)[0].content
    assert "was not approved" in results(events)[0].content
    assert results(events)[0].status == "failed"


@pytest.mark.parametrize(("name", "args"), [
    ("list_dir", lambda out: {"path": str(out)}),
    ("grep", lambda out: {"pattern": "secret", "path": str(out)}),
])
def test_a_path_outside_the_workspace_is_read_once_approved(fake_ollama, ws, outside, name, args):
    client = fake_ollama([tool_reply((name, args(outside))), text_reply("ok")])
    approver = Scripted(Decision.ALLOW_ONCE)

    _, events = run(client, ws, approver)

    assert len(approver.requests) == 1
    assert approver.requests[0].scope == "outside_workspace"
    assert results(events)[0].status == "completed"
    assert "secret" in results(events)[0].content


# ---------------------------------------------------------------- git: repo config must not run commands

def _git(ws: Path, *args) -> None:
    subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True, text=True)


def _marker_command(marker: Path) -> str:
    """A shell command that creates `marker`, for git's sh on Windows. Forward slashes, no spaces in the path."""
    return f"touch {marker.as_posix()}"


@requires_git
def test_git_status_and_diff_do_not_run_fsmonitor_from_the_repo_config(repo, tmp_path):
    marker = tmp_path / "fsmonitor-ran.txt"
    with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(f"[core]\n\tfsmonitor = {_marker_command(marker)}\n")
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")

    call("git_status", repo)
    call("git_diff", repo)

    assert not marker.exists()


@requires_git
def test_git_diff_does_not_run_a_textconv_from_the_repo_config(repo, tmp_path):
    marker = tmp_path / "textconv-ran.txt"
    (repo / ".gitattributes").write_text("*.txt diff=evil\n", encoding="utf-8")
    with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(f'[diff "evil"]\n\ttextconv = {_marker_command(marker)}\n')
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "attributes")
    (repo / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = call("git_diff", repo)

    assert not marker.exists()
    assert "+three" in result


def _filter_repo(repo: Path, name: str, key: str, command: str, extra: str = "") -> None:
    """Commit `.gitattributes` setting `filter=name` on *.txt, with `[filter "name"] key = command` (and
    any `extra` lines) in the repo config, then modify a.txt so status and diff have to run the filter."""
    (repo / ".gitattributes").write_text(f"*.txt filter={name}\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "attributes")
    with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(f'[filter "{name}"]\n\t{key} = {command}\n{extra}')
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")


@requires_git
def test_git_status_and_diff_do_not_run_a_clean_filter_from_the_repo_config(repo, tmp_path):
    marker = tmp_path / "clean-ran.txt"
    # A clean filter must pass its input through, or the diff itself would be wrong.
    _filter_repo(repo, "evil", "clean", f"{_marker_command(marker)}; cat")

    call("git_status", repo)
    call("git_diff", repo)

    assert not marker.exists()


@requires_git
def test_git_tools_do_not_run_a_process_filter_from_the_repo_config(repo, tmp_path):
    marker = tmp_path / "process-ran.txt"
    _filter_repo(repo, "evil", "process", _marker_command(marker))

    call("git_status", repo)
    call("git_diff", repo)

    assert not marker.exists()


@requires_git
def test_git_tools_do_not_run_a_dotted_filter_name_from_the_repo_config(repo, tmp_path):
    marker = tmp_path / "dotted-ran.txt"
    _filter_repo(repo, "my.evil", "clean", f"{_marker_command(marker)}; cat")

    call("git_status", repo)
    call("git_diff", repo)

    assert not marker.exists()


@requires_git
def test_git_tools_do_not_run_a_filter_whose_name_contains_an_equals_sign(repo, tmp_path):
    # `-c filter.a=b.clean=` would set filter.a, so the clean filter of "a=b" used to run.
    marker = tmp_path / "equals-clean-ran.txt"
    _filter_repo(repo, "a=b", "clean", f"{_marker_command(marker)}; cat")

    call("git_status", repo)
    result = call("git_diff", repo)

    assert not marker.exists()
    assert "+changed" in result


@requires_git
def test_git_tools_do_not_run_a_process_filter_whose_name_contains_an_equals_sign(repo, tmp_path):
    marker = tmp_path / "equals-process-ran.txt"
    _filter_repo(repo, "a=b", "process", _marker_command(marker))

    call("git_status", repo)
    call("git_diff", repo)

    assert not marker.exists()


@requires_git
def test_git_diff_keeps_entries_already_in_the_environment_and_still_blocks_the_filter(repo, tmp_path, monkeypatch):
    marker = tmp_path / "inherited-env-clean-ran.txt"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Inherited Name")
    seen_envs = []
    real_run = subprocess.run

    def spy(args, **kwargs):
        if "diff" in args:
            seen_envs.append(kwargs["env"])
        return real_run(args, **kwargs)

    monkeypatch.setattr(read_tools.subprocess, "run", spy)
    _filter_repo(repo, "evil", "clean", f"{_marker_command(marker)}; cat")

    result = call("git_diff", repo)

    assert not marker.exists()
    assert "+changed" in result
    env = seen_envs[0]
    assert env["GIT_CONFIG_COUNT"] == "3"  # the inherited entry, then the clean and required overrides
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("user.name", "Inherited Name")
    assert {env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_KEY_2"]} == {"filter.evil.clean", "filter.evil.required"}


@requires_git
def test_a_local_required_filter_does_not_make_the_diff_fail(repo, tmp_path):
    # Git LFS installed with `git lfs install --local` sets required=true; a blanked filter must not fail.
    marker = tmp_path / "required-clean-ran.txt"
    _filter_repo(repo, "x", "clean", f"{_marker_command(marker)}; cat", extra="\trequired = true\n")

    result = call("git_diff", repo)

    assert not marker.exists()
    assert "+changed" in result
    assert not result.startswith("Error")


@requires_git
@pytest.mark.parametrize(("returncode", "exception"), [
    (2, None),
    (None, subprocess.TimeoutExpired("git", 30)),
    (None, OSError("cannot run git")),
])
@pytest.mark.parametrize("name", ["git_status", "git_diff"])
def test_git_does_not_run_when_the_filter_listing_fails(repo, monkeypatch, returncode, exception, name):
    real_run = subprocess.run
    other_calls = []

    def fake_run(args, **kwargs):
        if "--get-regexp" in args:
            if exception is not None:
                raise exception
            return subprocess.CompletedProcess(args, returncode, "", "fatal: bad config line 1")
        other_calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(read_tools.subprocess, "run", fake_run)

    result = call(name, repo)

    assert result.startswith("Error: could not read the git config, so git was not run")
    assert other_calls == []


@requires_git
def test_git_tools_do_not_find_a_repository_above_the_workspace(ws):
    # The parent of the workspace is a repository; the workspace itself is a plain folder.
    parent = ws.parent
    _git(parent, "init", "-q")
    (parent / "outer.txt").write_text("outer\n", encoding="utf-8")

    for name in ("git_status", "git_diff", "git_log"):
        result = call(name, ws)
        assert result.startswith("The workspace is not a git repository: "), (name, result)
