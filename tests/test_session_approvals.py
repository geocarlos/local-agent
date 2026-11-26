"""Allow always for this session: scoped to one tool in one workspace, never to an outside path, empty for a new session.

Each test runs real turns through the loop with FakeOllama and a scripted approver.
"""

import os
from pathlib import Path

from fakes import text_reply, tool_reply

import pytest

from wildcat_code_agent import sandbox, tools
from wildcat_code_agent.config import Settings
from wildcat_code_agent.core import loop
from wildcat_code_agent.core.events import ApprovalRequest, Decision, ToolResult
from wildcat_code_agent.permissions import SessionApprovals
from wildcat_code_agent.tools.registry import ToolContext


class Scripted:
    """Approver that answers from a list (default: refuse) and records each request."""

    def __init__(self, *decisions: Decision):
        self.decisions = list(decisions)
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> Decision:
        self.requests.append(request)
        return self.decisions.pop(0) if self.decisions else Decision.REJECT_ONCE


def turn(client, ws: Path, approver, session=None, text="go") -> list:
    """Run one user turn in workspace `ws` and return its events."""
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": text})
    loop.run_turn(messages, client=client, settings=Settings(workspace=ws), emit=events.append,
                  approver=approver, session=session)
    return events


def results(events) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


def requests_of(events) -> list[ApprovalRequest]:
    return [e for e in events if isinstance(e, ApprovalRequest)]


# ---------------------------------------------------------------- the object itself

def test_a_new_session_allows_nothing():
    assert SessionApprovals().allows("write_file", Path.cwd()) is False


def test_allow_is_scoped_to_the_tool_and_the_workspace(tmp_path):
    session = SessionApprovals()
    session.allow("write_file", tmp_path)

    assert session.allows("write_file", tmp_path) is True
    assert session.allows("edit_file", tmp_path) is False
    assert session.allows("write_file", tmp_path.parent) is False


def test_the_same_folder_spelled_another_way_is_the_same_workspace(tmp_path):
    session = SessionApprovals()
    session.allow("write_file", tmp_path)

    assert session.allows("write_file", str(tmp_path) + os.sep) is True
    assert session.allows("write_file", str(tmp_path / ".")) is True


def test_allow_always_counts_as_allowed():
    assert Decision.ALLOW_ALWAYS.allowed is True
    assert Decision.REJECT_ONCE.allowed is False


# ---------------------------------------------------------------- through the loop

def test_allow_always_lets_the_same_tool_run_again_without_asking(fake_ollama, tmp_path):
    first, second = tmp_path / "one.txt", tmp_path / "two.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(first), "content": "1"})),
        tool_reply(("write_file", {"path": str(second), "content": "2"})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.ALLOW_ALWAYS)

    events = turn(client, tmp_path, approver, session=SessionApprovals())

    assert len(approver.requests) == 1
    assert first.read_text(encoding="utf-8") == "1"
    assert second.read_text(encoding="utf-8") == "2"
    assert [r.status for r in results(events)] == ["completed", "completed"]


def test_allow_always_carries_over_to_the_next_turn_of_the_same_session(fake_ollama, tmp_path):
    session = SessionApprovals()
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(tmp_path / "a.txt"), "content": "a"})),
        text_reply("first done"),
        tool_reply(("write_file", {"path": str(tmp_path / "b.txt"), "content": "b"})),
        text_reply("second done"),
    ])
    approver = Scripted(Decision.ALLOW_ALWAYS)

    turn(client, tmp_path, approver, session=session, text="first")
    turn(client, tmp_path, approver, session=session, text="second")

    assert len(approver.requests) == 1
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "b"


def test_a_different_tool_still_asks(fake_ollama, tmp_path):
    session = SessionApprovals()
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(tmp_path / "a.txt"), "content": "a"})),
        tool_reply(("run_command", {"command": "Write-Output hi"})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.ALLOW_ALWAYS, Decision.REJECT_ONCE)

    events = turn(client, tmp_path, approver, session=session)

    assert [r.tool_name for r in requests_of(events)] == ["write_file", "run_command"]
    assert results(events)[1].content == "User denied command execution."


def test_the_same_tool_in_another_workspace_still_asks(fake_ollama, tmp_path):
    ws_one, ws_two = tmp_path / "one", tmp_path / "two"
    ws_one.mkdir()
    ws_two.mkdir()
    session = SessionApprovals()
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(ws_one / "a.txt"), "content": "a"})),
        text_reply("done in one"),
        tool_reply(("write_file", {"path": str(ws_two / "b.txt"), "content": "b"})),
        text_reply("done in two"),
    ])
    approver = Scripted(Decision.ALLOW_ALWAYS, Decision.REJECT_ONCE)

    turn(client, ws_one, approver, session=session, text="one")
    events = turn(client, ws_two, approver, session=session, text="two")

    assert len(approver.requests) == 2
    assert results(events)[0].content == "User denied file write."
    assert not (ws_two / "b.txt").exists()


def test_a_new_session_starts_empty(fake_ollama, tmp_path):
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(tmp_path / "a.txt"), "content": "a"})),
        text_reply("done"),
        tool_reply(("write_file", {"path": str(tmp_path / "b.txt"), "content": "b"})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.ALLOW_ALWAYS, Decision.REJECT_ONCE)

    turn(client, tmp_path, approver, session=SessionApprovals())
    events = turn(client, tmp_path, approver, session=SessionApprovals())

    assert len(approver.requests) == 2
    assert results(events)[0].status == "failed"


def test_without_a_session_argument_the_allowance_lasts_for_one_turn_only(fake_ollama, tmp_path):
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(tmp_path / "a.txt"), "content": "a"})),
        text_reply("first done"),
        tool_reply(("write_file", {"path": str(tmp_path / "b.txt"), "content": "b"})),
        text_reply("second done"),
    ])
    approver = Scripted(Decision.ALLOW_ALWAYS, Decision.REJECT_ONCE)

    turn(client, tmp_path, approver, session=None, text="first")
    events = turn(client, tmp_path, approver, session=None, text="second")

    assert len(approver.requests) == 2  # no session object: the next turn starts with nothing allowed
    assert (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()
    assert results(events)[0].status == "failed"


def test_allow_once_is_not_remembered(fake_ollama, tmp_path):
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(tmp_path / "a.txt"), "content": "a"})),
        tool_reply(("write_file", {"path": str(tmp_path / "b.txt"), "content": "b"})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.ALLOW_ONCE, Decision.ALLOW_ONCE)

    turn(client, tmp_path, approver, session=SessionApprovals())

    assert len(approver.requests) == 2


def test_a_refusal_is_not_remembered(fake_ollama, tmp_path):
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(tmp_path / "a.txt"), "content": "a"})),
        tool_reply(("write_file", {"path": str(tmp_path / "b.txt"), "content": "b"})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.REJECT_ONCE, Decision.ALLOW_ONCE)

    events = turn(client, tmp_path, approver, session=SessionApprovals())

    assert len(approver.requests) == 2
    assert results(events)[0].status == "failed"
    assert (tmp_path / "b.txt").exists()


def test_the_request_offers_allow_always_for_a_tool_and_not_for_an_outside_path(fake_ollama, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(outside), "content": "x"})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.ALLOW_ONCE, Decision.REJECT_ONCE)

    turn(client, workspace, approver, session=SessionApprovals())

    outside_request, tool_request = approver.requests
    assert outside_request.scope == "outside_workspace"
    assert outside_request.options == ("allow_once", "reject_once")
    assert tool_request.scope == "tool"
    assert tool_request.options == ("allow_once", "allow_always", "reject_once")


def test_an_allowance_does_not_skip_the_outside_workspace_question(fake_ollama, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    session = SessionApprovals()
    session.allow("write_file", workspace)
    outside = tmp_path / "outside.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(outside), "content": "x"})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.REJECT_ONCE)

    events = turn(client, workspace, approver, session=session)

    assert [r.scope for r in requests_of(events)] == ["outside_workspace"]
    assert not outside.exists()
    assert results(events)[0].status == "failed"


def test_allow_always_answered_to_an_outside_path_is_only_allow_once(fake_ollama, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    session = SessionApprovals()
    outside = tmp_path / "outside.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(outside), "content": "x"})),
        tool_reply(("write_file", {"path": str(outside), "content": "y"})),
        text_reply("done"),
    ])
    # The outside question does not offer allow_always; an answer of it counts as allow once,
    # and the write itself still asks (the tool is not allowed for the session).
    approver = Scripted(Decision.ALLOW_ALWAYS, Decision.ALLOW_ONCE, Decision.REJECT_ONCE)

    events = turn(client, workspace, approver, session=session)

    assert [r.scope for r in requests_of(events)[:3]] == ["outside_workspace", "tool", "outside_workspace"]
    assert not session.allows("write_file", workspace)
    assert outside.read_text(encoding="utf-8") == "x"
    assert results(events)[1].status == "failed"


def test_an_approver_that_answers_with_an_unknown_value_refuses(fake_ollama, tmp_path):
    class Odd:
        def decide(self, request):
            return "maybe"

    client = fake_ollama([
        tool_reply(("write_file", {"path": str(tmp_path / "a.txt"), "content": "a"})),
        text_reply("done"),
    ])
    session = SessionApprovals()

    events = turn(client, tmp_path, Odd(), session=session)

    assert results(events)[0].status == "failed"
    assert not (tmp_path / "a.txt").exists()
    assert not session.allows("write_file", tmp_path)


# ---------------------------------------------------------------- writes under .git ask every time

@pytest.mark.parametrize("target", [".git/hooks/pre-commit", ".GIT/config", "sub/.git/x.txt"])
def test_a_session_allowance_does_not_cover_a_write_under_git(fake_ollama, tmp_path, target):
    session = SessionApprovals()
    session.allow("write_file", tmp_path)
    client = fake_ollama([
        tool_reply(("write_file", {"path": target, "content": "x"})),
        tool_reply(("write_file", {"path": target, "content": "y"})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.ALLOW_ALWAYS, Decision.ALLOW_ONCE)

    events = turn(client, tmp_path, approver, session=session)

    assert [r.scope for r in requests_of(events)] == ["tool", "tool"]
    assert all("allow_always" not in r.options for r in requests_of(events))
    assert (tmp_path / target).read_text(encoding="utf-8") == "y"


def test_an_edit_under_git_asks_every_time_and_offers_no_allow_always(fake_ollama, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("a = 1\n", encoding="utf-8")
    session = SessionApprovals()
    session.allow("edit_file", tmp_path)
    client = fake_ollama([
        tool_reply(("edit_file", {"path": ".git/config", "old_text": "a = 1", "new_text": "a = 2"})),
        tool_reply(("edit_file", {"path": ".git/config", "old_text": "a = 2", "new_text": "a = 3"})),
        text_reply("done"),
    ])
    approver = Scripted(Decision.ALLOW_ONCE, Decision.ALLOW_ONCE)

    events = turn(client, tmp_path, approver, session=session)

    assert len(requests_of(events)) == 2
    assert all(r.options == ("allow_once", "reject_once") for r in requests_of(events))
    assert (tmp_path / ".git" / "config").read_text(encoding="utf-8") == "a = 3\n"


def test_an_ordinary_file_next_to_git_still_uses_the_allowance(fake_ollama, tmp_path):
    session = SessionApprovals()
    session.allow("write_file", tmp_path)
    client = fake_ollama([
        tool_reply(("write_file", {"path": ".gitignore", "content": "x"})),
        text_reply("done"),
    ])

    events = turn(client, tmp_path, Scripted(), session=session)

    assert requests_of(events) == []
    assert results(events)[0].status == "completed"


def test_a_write_under_git_offers_no_allow_always_even_without_a_session(tmp_path):
    request = tools.TOOLS["write_file"].approval_request(
        "1.1", {"path": str(tmp_path / ".git" / "hooks" / "pre-commit"), "content": "x"},
        ToolContext(workspace=tmp_path))

    assert request.options == ("allow_once", "reject_once")


@pytest.mark.parametrize("path, under_git", [
    ("C:/w/.git/config", True),
    ("C:/w/.GIT/hooks/x", True),
    ("C:/w/.gitignore", False),
    ("C:/w/git/config", False),
    ("C:/w/sub/.github/x.yml", False),
])
def test_only_a_path_component_named_git_counts_as_git(path, under_git):
    assert sandbox.is_git_path(path) is under_git


@pytest.mark.parametrize("answer", [None, True, 1])
def test_an_approver_that_returns_a_non_string_refuses(fake_ollama, tmp_path, answer):
    class NotAString:
        def decide(self, request):
            return answer

    client = fake_ollama([
        tool_reply(("write_file", {"path": str(tmp_path / "a.txt"), "content": "a"})),
        text_reply("done"),
    ])
    session = SessionApprovals()

    events = turn(client, tmp_path, NotAString(), session=session)

    assert results(events)[0].status == "failed"
    assert results(events)[0].content == "User denied file write."
    assert not (tmp_path / "a.txt").exists()
    assert not session.allows("write_file", tmp_path)
