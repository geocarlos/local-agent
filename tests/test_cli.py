"""Terminal frontend: y/n approver and event rendering. `input` is monkeypatched."""

import pytest
from fakes import text_reply, tool_reply

from local_agent.core import loop
from local_agent.core.events import ApprovalRequest, Decision, Preview, TurnDone
from local_agent.frontends import cli


def request_for(title="Write 2 chars to 'a.txt'") -> ApprovalRequest:
    return ApprovalRequest(tool_call_id="1.1", tool_name="write_file", risk="write", title=title)


@pytest.mark.parametrize("answer", ["y", "Y", " y \n"])
def test_y_grants(monkeypatch, answer):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or answer)

    assert cli.TerminalApprover().decide(request_for()) is Decision.ALLOW_ONCE
    assert prompts == ["\n[APPROVE] Write 2 chars to 'a.txt'? (y/n): "]


@pytest.mark.parametrize("answer", ["n", "", "yes", "no"])
def test_anything_else_denies(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)

    assert cli.TerminalApprover().decide(request_for()) is Decision.REJECT_ONCE


def test_render_write_request_shows_proposed_content(capsys):
    cli.render(ApprovalRequest(
        tool_call_id="1.1",
        tool_name="write_file",
        risk="write",
        title="Write 5 chars to 'a.txt'",
        preview=Preview(text="hello", path="a.txt"),
    ))

    assert capsys.readouterr().out == (
        "\n[PROPOSED CONTENT for 'a.txt']\nhello\n[END PROPOSED CONTENT]\n"
    )


def test_render_exec_request_shows_no_extra_block(capsys):
    cli.render(ApprovalRequest(
        tool_call_id="1.1",
        tool_name="run_command",
        risk="exec",
        title="Run command `dir`",
        preview=Preview(text="dir", kind="command"),
    ))

    assert capsys.readouterr().out == ""


def test_render_max_rounds_message(capsys):
    cli.render(TurnDone(stop_reason="max_turn_requests", rounds=10))

    assert capsys.readouterr().out == (
        "\nAgent: stopped after 10 tool rounds without a final answer. "
        "Ask again or narrow the request.\n"
    )


def test_repl_write_with_y_then_exit(fake_ollama, monkeypatch, tmp_path, capsys):
    target = tmp_path / "a.txt"
    answers = iter(["write it", "y", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("saved it"),
    ])

    cli.run(client=client)

    out = capsys.readouterr().out
    assert f"Local Agent ready ({loop.MODEL})" in out
    assert "[Tool Call] write_file(" in out
    assert f"[PROPOSED CONTENT for '{target}']\nhi\n[END PROPOSED CONTENT]" in out
    assert "Agent: saved it" in out
    assert target.read_text(encoding="utf-8") == "hi"


def test_repl_write_refused_with_n(fake_ollama, monkeypatch, tmp_path, capsys):
    target = tmp_path / "a.txt"
    answers = iter(["write it", "n", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("I did not write it"),
    ])

    cli.run(client=client)

    assert not target.exists()
    assert client.calls[1]["messages"][-1] == {"role": "tool", "content": "User denied file write."}
    assert "Agent: I did not write it" in capsys.readouterr().out


def test_cli_repl_runs_a_turn_and_exits(fake_ollama, monkeypatch, capsys):
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = fake_ollama([text_reply("hi there")])

    cli.run(client=client)

    out = capsys.readouterr().out
    assert f"Local Agent ready ({loop.MODEL})" in out
    assert "Agent: hi there" in out
    assert client.calls[0]["messages"][-1] == {"role": "user", "content": "hello"}
