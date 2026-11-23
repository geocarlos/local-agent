"""Approval flow through the agent loop: write and exec granted or denied, reads not asking."""

import sys

from fakes import text_reply, tool_reply

from local_agent.core import loop
from local_agent.core.events import ApprovalRequest, Decision, ToolCall, ToolResult
from local_agent.tools import builtin


class RecordingApprover:
    """Records every request and answers with a fixed decision."""

    def __init__(self, decision: Decision):
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> Decision:
        self.requests.append(request)
        return self.decision


def start(user_text: str) -> list:
    messages = loop.new_messages()
    messages.append({"role": "user", "content": user_text})
    return messages


def run(client, approver=None):
    events: list = []
    messages = start("do it")
    loop.run_turn(messages, client=client, emit=events.append, approver=approver)
    return messages, events


def result_of(events) -> ToolResult:
    return next(e for e in events if isinstance(e, ToolResult))


def test_write_granted_writes_the_file(fake_ollama, tmp_path):
    target = tmp_path / "out.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("saved"),
    ])
    approver = RecordingApprover(Decision.ALLOW_ONCE)

    _, events = run(client, approver)

    assert target.read_text(encoding="utf-8") == "hi"
    assert len(approver.requests) == 1
    request = approver.requests[0]
    assert request.tool_name == "write_file"
    assert request.title == f"Write 2 chars to '{target}'"
    assert request.preview.path == str(target)
    assert request.preview.text == "hi"
    assert result_of(events).content == f"Successfully wrote to {target}."
    assert result_of(events).status == "completed"


def test_write_denied_leaves_the_file_alone(fake_ollama, tmp_path):
    target = tmp_path / "out.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("ok, not written"),
    ])
    approver = RecordingApprover(Decision.REJECT_ONCE)

    messages, events = run(client, approver)

    assert not target.exists()
    assert len(approver.requests) == 1
    assert result_of(events).content == "User denied file write."
    assert result_of(events).status == "failed"
    assert client.calls[1]["messages"][-1] == {
        "role": "tool", "content": "User denied file write.", "tool_name": "write_file"
    }


def test_exec_granted_runs_the_command(fake_ollama):
    command = f'"{sys.executable}" -c "print(\'pong\')"'
    client = fake_ollama([
        tool_reply(("run_command", {"command": command})),
        text_reply("it printed pong"),
    ])
    approver = RecordingApprover(Decision.ALLOW_ONCE)

    _, events = run(client, approver)

    assert len(approver.requests) == 1
    assert approver.requests[0].title == f"Run command `{command}`"
    assert approver.requests[0].preview.text == command
    content = result_of(events).content
    assert content.startswith("EXIT CODE: 0")
    assert "pong" in content
    assert result_of(events).status == "completed"


def test_exec_denied_never_runs(fake_ollama, monkeypatch):
    def fail_if_run(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when the command is denied")

    monkeypatch.setattr(builtin.subprocess, "run", fail_if_run)
    client = fake_ollama([
        tool_reply(("run_command", {"command": "echo should-not-run"})),
        text_reply("understood"),
    ])
    approver = RecordingApprover(Decision.REJECT_ONCE)

    _, events = run(client, approver)

    assert len(approver.requests) == 1
    assert result_of(events).content == "User denied command execution."
    assert result_of(events).status == "failed"


def test_read_does_not_ask(fake_ollama, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("body", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("read_file", {"path": str(target)})),
        text_reply("body"),
    ])
    approver = RecordingApprover(Decision.REJECT_ONCE)

    _, events = run(client, approver)

    assert approver.requests == []
    assert result_of(events).content == "body"


def test_without_an_approver_risky_tools_are_refused(fake_ollama, tmp_path):
    target = tmp_path / "out.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "x"})),
        text_reply("done"),
    ])

    _, events = run(client, approver=None)

    assert not target.exists()
    assert result_of(events).content == "User denied file write."


def test_event_order_for_a_turn_with_a_write(fake_ollama, tmp_path):
    target = tmp_path / "out.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "x"})),
        text_reply("saved"),
    ])

    _, events = run(client, RecordingApprover(Decision.ALLOW_ONCE))

    assert [e.type for e in events] == [
        "tool_call",
        "approval_request",
        "tool_result",
        "text_delta",
        "turn_done",
    ]
    call, request, result = events[0], events[1], events[2]
    assert isinstance(call, ToolCall) and call.kind == "edit"
    assert request.tool_call_id == call.tool_call_id == result.tool_call_id
    assert events[-1].stop_reason == "end_turn"


def test_event_order_for_a_turn_with_a_read(fake_ollama, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("body", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("read_file", {"path": str(target)})),
        text_reply("body"),
    ])

    _, events = run(client, RecordingApprover(Decision.REJECT_ONCE))

    assert [e.type for e in events] == ["tool_call", "tool_result", "text_delta", "turn_done"]


def test_unknown_tool_is_reported_as_failed_without_approval(fake_ollama):
    client = fake_ollama([
        tool_reply(("no_such_tool", {})),
        text_reply("ok"),
    ])
    approver = RecordingApprover(Decision.ALLOW_ONCE)

    _, events = run(client, approver)

    assert approver.requests == []
    assert result_of(events).content == "Unknown tool: no_such_tool"
    assert result_of(events).status == "failed"


def test_extra_argument_is_rejected_before_asking(fake_ollama, tmp_path):
    target = tmp_path / "out.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "x", "zz": 1})),
        text_reply("retrying"),
    ])
    approver = RecordingApprover(Decision.ALLOW_ONCE)

    _, events = run(client, approver)

    assert approver.requests == []
    assert not target.exists()
    content = result_of(events).content
    assert content.startswith("Error calling write_file with arguments")
    assert "write_file() got an unexpected keyword argument 'zz'" in content
    assert result_of(events).status == "failed"


def test_missing_argument_is_rejected_before_asking(fake_ollama, tmp_path):
    target = tmp_path / "out.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target)})),
        text_reply("retrying"),
    ])
    approver = RecordingApprover(Decision.ALLOW_ONCE)

    _, events = run(client, approver)

    assert approver.requests == []
    assert not target.exists()
    content = result_of(events).content
    assert "write_file()" in content
    assert "'content'" in content
    assert result_of(events).status == "failed"


class RaisingApprover:
    def decide(self, request):
        raise RuntimeError("approval UI crashed")


def test_approver_that_raises_refuses_and_does_not_run(fake_ollama, tmp_path, monkeypatch):
    def fail_if_run(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when the approver fails")

    monkeypatch.setattr(builtin.subprocess, "run", fail_if_run)
    target = tmp_path / "out.txt"
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "x"})),
        tool_reply(("run_command", {"command": "echo nope"})),
        text_reply("ok"),
    ])

    _, events = run(client, RaisingApprover())

    assert not target.exists()
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.content for r in results] == ["User denied file write.", "User denied command execution."]
    assert all(r.status == "failed" for r in results)
