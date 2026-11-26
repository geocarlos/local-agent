"""git_commit against a temp repository: what is committed, the refusals before the question, the repo code that does not run, and outside paths.

The repository is created in the test (git must be installed). Identity is set in the repo config,
except where a test removes it through the environment.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fakes import FakeOllama, text_reply, tool_reply

from wildcat_code_agent import tools
from wildcat_code_agent.config import Settings
from wildcat_code_agent.core import loop
from wildcat_code_agent.core.events import ApprovalRequest, Decision, ToolResult
from wildcat_code_agent.permissions import SessionApprovals
from wildcat_code_agent.tools import git_write, read_tools
from wildcat_code_agent.tools.registry import ToolContext

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


class Scripted:
    """Approver that answers from a list (default: refuse) and records each request."""

    def __init__(self, *decisions: Decision):
        self.decisions = list(decisions)
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> Decision:
        self.requests.append(request)
        return self.decisions.pop(0) if self.decisions else Decision.REJECT_ONCE


def git(ws: Path, *args, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, encoding="utf-8")
    if check:
        assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def commit_call(ws: Path, **args) -> str:
    return tools.TOOLS["git_commit"].call(args, ToolContext(workspace=ws))


def precheck(ws: Path, **args):
    return tools.TOOLS["git_commit"].precheck(args, ToolContext(workspace=ws))


def run_loop(client, ws: Path, approver):
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "commit"})
    loop.run_turn(messages, client=client, settings=Settings(workspace=ws), emit=events.append,
                  approver=approver)
    return messages, events


def result_of(events) -> ToolResult:
    return next(e for e in events if isinstance(e, ToolResult))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A workspace that is a git repository with one commit (a.txt), identity set in the repo config."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    ws = tmp_path / "work"
    ws.mkdir()
    monkeypatch.chdir(tmp_path)  # the process cwd is not the workspace
    git(ws, "init", "-q")
    git(ws, "config", "user.name", "Test User")
    git(ws, "config", "user.email", "test@example.invalid")
    git(ws, "config", "commit.gpgsign", "false")
    (ws / "a.txt").write_text("one\n", encoding="utf-8")
    git(ws, "add", "a.txt")
    git(ws, "commit", "-q", "-m", "first")
    return ws


def files_in_head(ws: Path) -> list[str]:
    return git(ws, "show", "--name-only", "--format=", "HEAD").split()


# ---------------------------------------------------------------- what gets committed

@requires_git
def test_commits_what_is_staged(repo):
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    git(repo, "add", "b.txt")

    result = commit_call(repo, message="add b")

    assert "add b" in result
    assert git(repo, "log", "-1", "--format=%s") == "add b"
    assert files_in_head(repo) == ["b.txt"]


@requires_git
def test_nothing_staged_is_refused_before_the_question(fake_ollama, repo):
    (repo / "b.txt").write_text("untracked\n", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "nothing"})),
        text_reply("ok"),
    ])
    approver = Scripted(Decision.ALLOW_ONCE)

    _, events = run_loop(client, repo, approver)

    assert approver.requests == []
    assert result_of(events).status == "failed"
    assert "nothing is staged to commit" in result_of(events).content
    assert git(repo, "log", "-1", "--format=%s") == "first"


@requires_git
def test_paths_stage_and_commit_only_those_files(fake_ollama, repo):
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.txt")  # already staged by the user: not part of this commit
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "just b", "paths": ["b.txt"]})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.ALLOW_ONCE)

    _, events = run_loop(client, repo, approver)

    assert result_of(events).status == "completed"
    assert files_in_head(repo) == ["b.txt"]
    assert "c.txt" in git(repo, "diff", "--cached", "--name-only")  # still staged, not committed
    assert "?? c.txt" not in git(repo, "status", "--short")


@requires_git
def test_paths_with_no_changes_are_refused_before_the_question(fake_ollama, repo):
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "nothing", "paths": ["a.txt"]})),
        text_reply("ok"),
    ])
    approver = Scripted(Decision.ALLOW_ONCE)

    _, events = run_loop(client, repo, approver)

    assert approver.requests == []
    assert "none of the given paths has changes" in result_of(events).content


@requires_git
def test_a_single_path_string_is_treated_as_one_path(repo):
    (repo / "b.txt").write_text("b\n", encoding="utf-8")

    assert precheck(repo, message="m", paths="b.txt") is None
    result = commit_call(repo, message="only b", paths="b.txt")

    assert not result.startswith("Error")
    assert files_in_head(repo) == ["b.txt"]


# ---------------------------------------------------------------- refusals before the question

@requires_git
def test_an_empty_message_is_refused(repo):
    assert "message is empty" in precheck(repo, message="   ")


@requires_git
def test_missing_identity_is_refused_before_the_question(fake_ollama, repo, monkeypatch):
    # An empty name in the environment overrides the repo config, so git cannot form the committer identity.
    monkeypatch.setenv("GIT_COMMITTER_NAME", "")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(repo, "add", "b.txt")
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "no identity"})),
        text_reply("ok"),
    ])
    approver = Scripted(Decision.ALLOW_ONCE)

    _, events = run_loop(client, repo, approver)

    assert approver.requests == []
    assert "no committer name or email" in result_of(events).content
    assert git(repo, "log", "-1", "--format=%s") == "first"


@requires_git
def test_outside_the_repository_says_so(tmp_path):
    ws = tmp_path / "plain"
    ws.mkdir()

    assert "not a git repository" in precheck(ws, message="m")


# ---------------------------------------------------------------- approval preview

@requires_git
def test_the_preview_lists_the_commit_and_says_no_repo_code_runs(repo):
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(repo, "add", "b.txt")

    request = tools.TOOLS["git_commit"].approval_request(
        "1.1", {"message": "add b\n\nlonger body"}, ToolContext(workspace=repo))

    assert request.title == 'Commit staged changes with message "add b"'
    assert request.preview.kind == "diff"
    assert "Message:\nadd b\n\nlonger body" in request.preview.text
    assert "b.txt" in request.preview.text  # the staged diffstat
    assert git_write.HOOKS_NOTE in request.preview.text


@requires_git
def test_the_preview_for_paths_shows_their_status_without_staging_them(repo):
    (repo / "b.txt").write_text("b\n", encoding="utf-8")

    request = tools.TOOLS["git_commit"].approval_request(
        "1.1", {"message": "m", "paths": ["b.txt"]}, ToolContext(workspace=repo))

    assert request.title == 'Commit 1 path(s) with message "m"'
    assert "?? b.txt" in request.preview.text
    assert git(repo, "diff", "--cached", "--name-only") == ""  # previewing stages nothing


# ---------------------------------------------------------------- git_commit runs no repository code

def _marker_command(marker: Path) -> str:
    """A shell command that creates `marker`, for git's sh on Windows. Forward slashes, no spaces in the path."""
    return f"touch {marker.as_posix()}"


@requires_git
def test_a_pre_commit_hook_does_not_run_during_git_commit(repo, tmp_path):
    marker = tmp_path / "hook-ran.txt"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\n{_marker_command(marker)}\n", encoding="utf-8", newline="\n")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(repo, "add", "b.txt")

    commit_call(repo, message="hooked")

    assert "hooked" in git(repo, "log", "-1", "--format=%s")
    assert not marker.exists()


@requires_git
def test_fsmonitor_and_a_clean_filter_from_the_repo_do_not_run_during_git_commit(repo, tmp_path):
    fsmonitor_marker = tmp_path / "fsmonitor-ran.txt"
    clean_marker = tmp_path / "clean-ran.txt"
    with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(f"[core]\n\tfsmonitor = {_marker_command(fsmonitor_marker)}\n")
        f.write(f'[filter "evil"]\n\tclean = {_marker_command(clean_marker)}\n')
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")

    # With paths, git_commit runs `git add` itself, which is where a clean filter would run.
    commit_call(repo, message="filtered", paths=["b.txt"])

    assert "filtered" in git(repo, "log", "-1", "--format=%s")
    assert not fsmonitor_marker.exists()
    assert not clean_marker.exists()


@requires_git
def test_a_repo_that_asks_for_a_signature_is_committed_without_signing(repo, tmp_path):
    marker = tmp_path / "gpg-ran.txt"
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", _marker_command(marker))
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(repo, "add", "b.txt")

    result = commit_call(repo, message="unsigned")

    assert "unsigned" in git(repo, "log", "-1", "--format=%s"), result
    assert not marker.exists()


@requires_git
def test_git_commit_is_refused_when_the_repo_config_cannot_be_read(repo, monkeypatch):
    monkeypatch.setattr(read_tools, "_repo_filter_overrides",
                        lambda *args: ({}, "Error: could not read the git config, so git was not run (timed out)."))
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(repo, "add", "b.txt")

    result = commit_call(repo, message="blocked")

    assert result.startswith("Error: git commit failed: could not read the git config")
    assert git(repo, "log", "-1", "--format=%s") == "first"


# ---------------------------------------------------------------- failures leave the paths staged

@requires_git
def test_a_failed_commit_after_add_leaves_the_paths_staged(repo, monkeypatch):
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    real = git_write._git_process

    def fail_the_commit(args, context, stdin_text):
        if args[0] == "commit":
            return False, "exit 1: simulated failure"
        return real(args, context, stdin_text)

    monkeypatch.setattr(git_write, "_git_process", fail_the_commit)

    result = commit_call(repo, message="blocked", paths=["b.txt"])

    assert result.startswith("Error: git commit failed: exit 1: simulated failure")
    assert "The paths are still staged." in result
    assert "b.txt" in git(repo, "diff", "--cached", "--name-only")


# ---------------------------------------------------------------- the session allowance does not apply

def test_git_commit_offers_no_allow_always(repo):
    request = tools.TOOLS["git_commit"].approval_request(
        "1.1", {"message": "m", "paths": ["a.txt"]}, ToolContext(workspace=repo))

    assert request.options == ("allow_once", "reject_once")


@requires_git
def test_a_yes_to_git_commit_is_never_remembered_for_the_session(fake_ollama, repo):
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    session = SessionApprovals()
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "one", "paths": ["b.txt"]})),
        tool_reply(("git_commit", {"message": "two", "paths": ["c.txt"]})),
        text_reply("ok"),
    ])
    approver = Scripted(Decision.ALLOW_ALWAYS, Decision.ALLOW_ALWAYS)
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "commit"})

    loop.run_turn(messages, client=client, settings=Settings(workspace=repo), emit=events.append,
                  approver=approver, session=session)

    assert len(approver.requests) == 2
    assert not session.allows("git_commit", repo)
    assert git(repo, "log", "-1", "--format=%s") == "two"


@requires_git
def test_an_earlier_session_allowance_for_git_commit_is_not_used(fake_ollama, repo):
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    session = SessionApprovals()
    session.allow("git_commit", repo)
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "asked", "paths": ["b.txt"]})),
        text_reply("ok"),
    ])
    approver = Scripted(Decision.REJECT_ONCE)
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "commit"})

    loop.run_turn(messages, client=client, settings=Settings(workspace=repo), emit=events.append,
                  approver=approver, session=session)

    assert len(approver.requests) == 1
    assert result_of(events).content == "User denied git commit."


# ---------------------------------------------------------------- outside-workspace paths

@requires_git
def test_a_path_outside_the_workspace_asks_first_and_a_refusal_commits_nothing(fake_ollama, repo, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x\n", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "outside", "paths": [str(outside)]})),
        text_reply("ok"),
    ])
    approver = Scripted(Decision.REJECT_ONCE)

    _, events = run_loop(client, repo, approver)

    assert len(approver.requests) == 1
    assert approver.requests[0].scope == "outside_workspace"
    assert result_of(events).content.startswith("Access to")
    assert git(repo, "log", "-1", "--format=%s") == "first"


@requires_git
def test_a_session_allowance_for_git_commit_does_not_cover_an_outside_path(fake_ollama, tmp_path, repo):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x\n", encoding="utf-8")
    session = SessionApprovals()
    session.allow("git_commit", repo)
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "x", "paths": [str(outside)]})),
        text_reply("ok"),
    ])
    approver = Scripted(Decision.REJECT_ONCE)
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "commit"})

    loop.run_turn(messages, client=client, settings=Settings(workspace=repo), emit=events.append,
                  approver=approver, session=session)

    assert [r.scope for r in approver.requests] == ["outside_workspace"]
    assert result_of(events).status == "failed"


# ---------------------------------------------------------------- repo-defined filters (paths are refused)

FILTER_REFUSAL = ("Error: These paths use a repo-defined filter (x): big.txt. Stage them yourself with "
                  "`git add`, then call git_commit without paths. Nothing was committed.")


def _pointer_filter_repo(repo: Path) -> None:
    """A clean filter defined in the repo, as `git lfs install --local` leaves one: *.txt is stored as a pointer."""
    git(repo, "config", "filter.x.clean", "echo POINTER:13")
    (repo / ".gitattributes").write_text("*.txt filter=x\n", encoding="utf-8")
    (repo / "big.txt").write_text("REAL CONTENT\n", encoding="utf-8")


@requires_git
def test_paths_through_a_repo_filter_are_refused_before_the_question(fake_ollama, repo):
    _pointer_filter_repo(repo)
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "big", "paths": ["big.txt"]})),
        text_reply("ok"),
    ])
    approver = Scripted(Decision.ALLOW_ONCE)

    _, events = run_loop(client, repo, approver)

    assert approver.requests == []
    assert result_of(events).content == FILTER_REFUSAL
    assert git(repo, "log", "-1", "--format=%s") == "first"


@requires_git
def test_a_wildcard_path_is_refused_when_the_repo_defines_a_filter(repo):
    _pointer_filter_repo(repo)

    assert precheck(repo, message="all", paths=["*.txt"]) == (
        "Error: These paths use a repo-defined filter (x): *.txt. Stage them yourself with "
        "`git add`, then call git_commit without paths. Nothing was committed.")


@requires_git
def test_staging_by_hand_and_committing_without_paths_stores_the_filtered_blob(repo):
    _pointer_filter_repo(repo)
    git(repo, "add", "big.txt")

    commit_call(repo, message="big")

    assert files_in_head(repo) == ["big.txt"]
    assert git(repo, "show", "HEAD:big.txt") == "POINTER:13"


@requires_git
def test_a_path_the_filter_does_not_cover_still_commits_in_a_repo_with_the_filter(fake_ollama, repo):
    _pointer_filter_repo(repo)
    (repo / "plain.bin").write_text("plain\n", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("git_commit", {"message": "plain", "paths": ["plain.bin"]})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.ALLOW_ONCE)

    _, events = run_loop(client, repo, approver)

    assert len(approver.requests) == 1  # the path went through the loop, so the sandbox may have made it absolute
    assert result_of(events).status == "completed"
    assert git(repo, "log", "-1", "--format=%s") == "plain"
    assert files_in_head(repo) == ["plain.bin"]


@requires_git
def test_a_filter_defined_only_in_the_global_config_does_not_refuse_paths(repo, tmp_path, monkeypatch):
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text('[filter "x"]\n\tclean = echo POINTER:13\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    (repo / ".gitattributes").write_text("*.txt filter=x\n", encoding="utf-8")
    (repo / "big.txt").write_text("REAL CONTENT\n", encoding="utf-8")

    assert precheck(repo, message="big", paths=["big.txt"]) is None
