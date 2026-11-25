"""Terminal frontend: y/n approver and event rendering. `input` is monkeypatched."""

import subprocess
import sys

import httpx
import ollama
import pytest
from fakes import FakeOllama, Raise, Stream, text_reply, tool_reply

from local_agent.config import DEFAULT_MODEL, Settings
from local_agent.core.events import (
    ApprovalRequest,
    Decision,
    Preview,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    TurnDone,
    Usage,
)
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


def test_repl_write_with_y_then_exit(fake_ollama, monkeypatch, workspace, capsys):
    target = workspace / "a.txt"
    answers = iter(["write it", "y", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("saved it"),
    ])

    cli.run(client=client)

    out = capsys.readouterr().out
    assert f"Local Agent ready ({DEFAULT_MODEL})" in out
    assert "[Tool Call] write_file(" in out
    assert f"[PROPOSED CONTENT for '{target}']\nhi\n[END PROPOSED CONTENT]" in out
    assert "Agent: saved it" in out
    assert target.read_text(encoding="utf-8") == "hi"


def test_repl_write_refused_with_n(fake_ollama, monkeypatch, workspace, capsys):
    target = workspace / "a.txt"
    answers = iter(["write it", "n", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("I did not write it"),
    ])

    cli.run(client=client)

    assert not target.exists()
    assert client.calls[1]["messages"][-1] == {
        "role": "tool", "content": "User denied file write.", "tool_name": "write_file"
    }
    assert "Agent: I did not write it" in capsys.readouterr().out


def test_cli_repl_runs_a_turn_and_exits(fake_ollama, monkeypatch, capsys):
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = fake_ollama([text_reply("hi there")])

    cli.run(client=client)

    out = capsys.readouterr().out
    assert f"Local Agent ready ({DEFAULT_MODEL})" in out
    assert "Agent: hi there" in out
    assert client.calls[0]["messages"][-1] == {"role": "user", "content": "hello"}


def test_renderer_keeps_one_agent_line_for_streamed_text(capsys):
    render = cli.Renderer()
    render(TextDelta(text="hel"))
    render(TextDelta(text="lo"))
    render(TurnDone(stop_reason="end_turn", text="hello", rounds=1))

    assert capsys.readouterr().out == "\nAgent: hello\n"


def test_renderer_closes_streamed_text_before_a_tool_call(capsys):
    render = cli.Renderer()
    render(TextDelta(text="checking"))
    render(ToolCall(tool_call_id="1.1", name="read_file", arguments={"path": "a"}))

    assert capsys.readouterr().out == "\nAgent: checking\n\n[Tool Call] read_file({'path': 'a'})\n"


def test_repl_streams_the_reply_in_one_agent_line(fake_ollama, monkeypatch, capsys):
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = fake_ollama([text_reply("hi there, friend")], chunk_chars=3)

    cli.run(client=client)

    assert "\nAgent: hi there, friend\n" in capsys.readouterr().out


def test_ctrl_c_during_a_turn_returns_to_the_prompt(fake_ollama, monkeypatch, tmp_path, capsys):
    target = tmp_path / "a.txt"
    answers = iter(["write it", KeyboardInterrupt(), "exit"])

    def fake_input(prompt=""):
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("unused"),
    ])

    cli.run(client=client)  # returns normally: "exit" was read after the interrupted turn

    out = capsys.readouterr().out
    assert not target.exists()
    assert "[Turn cancelled]" in out
    assert len(client.calls) == 1
    assert list(answers) == []


def test_ctrl_c_at_the_prompt_exits_cleanly(fake_ollama, monkeypatch, capsys):
    def interrupt(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)

    cli.run(client=fake_ollama([]))  # no KeyboardInterrupt escapes

    assert f"Local Agent ready ({DEFAULT_MODEL})" in capsys.readouterr().out


def test_ollama_down_shows_an_error_and_stays_at_the_prompt(monkeypatch, capsys):
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    cli.run(settings=Settings(host="http://127.0.0.1:1"), client=ollama.Client(host="http://127.0.0.1:1"))

    out = capsys.readouterr().out
    assert "Error: Cannot reach Ollama at http://127.0.0.1:1" in out
    assert "ollama serve" in out
    assert list(inputs) == []  # the second input was read: the REPL kept going


def test_token_line_shows_prompt_against_the_context_size(capsys):
    cli.render(TurnDone(stop_reason="end_turn", text="x", rounds=1, usage=Usage(1000, 50)))
    assert "[tokens: 1000 prompt, 50 generated]" in capsys.readouterr().out

    renderer = cli.Renderer(num_ctx=8192)
    renderer(TurnDone(stop_reason="end_turn", text="x", rounds=1, usage=Usage(1000, 50)))
    out = capsys.readouterr().out
    assert "[tokens: 1000 of 8192 prompt, 50 generated]" in out
    assert "Warning" not in out


def test_context_warning_when_the_prompt_nearly_fills_num_ctx(capsys):
    cli.Renderer(num_ctx=8192)(TurnDone(stop_reason="end_turn", text="x", rounds=1, usage=Usage(7500, 10)))

    out = capsys.readouterr().out
    assert "[tokens: 7500 of 8192 prompt" in out
    assert "the conversation fills 7500 of 8192 context tokens" in out
    assert "--num-ctx" in out


def test_no_token_line_when_ollama_gave_no_counts(capsys):
    cli.render(TurnDone(stop_reason="end_turn", text="x", rounds=1, usage=None))

    assert capsys.readouterr().out == ""


def test_thinking_shows_one_indicator_before_the_answer(capsys):
    render = cli.Renderer()
    render(ThinkingDelta(text="hmm"))
    render(ThinkingDelta(text="more"))
    render(TextDelta(text="hi"))
    render(TurnDone(stop_reason="end_turn", text="hi", rounds=1))

    assert capsys.readouterr().out == "\n[thinking...]\nAgent: hi\n"


def test_repl_sends_the_configured_num_ctx_and_model(fake_ollama, monkeypatch):
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = fake_ollama([text_reply("hi")])

    cli.run(settings=Settings(model="other:1b", num_ctx=4096), client=client)

    assert client.calls[0]["model"] == "other:1b"
    assert client.calls[0]["options"] == {"num_ctx": 4096}


def test_repl_shows_the_thinking_indicator_and_token_line(fake_ollama, monkeypatch, capsys):
    inputs = iter(["hello", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = fake_ollama([text_reply("hi there", thinking="pondering", prompt_eval_count=120, eval_count=7)])

    cli.run(settings=Settings(num_ctx=8192), client=client)

    out = capsys.readouterr().out
    assert "[thinking...]\nAgent: hi there" in out
    assert "[tokens: 120 of 8192 prompt, 7 generated]" in out


def test_a_failed_turn_is_not_repeated_in_the_history_on_retry(monkeypatch, capsys):
    inputs = iter(["first try", "second try", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = FakeOllama([Stream([Raise(httpx.ConnectError("refused"))]), text_reply("ok")])

    cli.run(settings=Settings(host="http://127.0.0.1:1"), client=client)

    assert "Error: Cannot reach Ollama" in capsys.readouterr().out
    user_texts = [m["content"] for m in client.calls[1]["messages"] if m["role"] == "user"]
    assert user_texts == ["second try"]


def test_a_turn_with_a_reply_keeps_its_user_message(fake_ollama, monkeypatch):
    inputs = iter(["one", "two", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    client = fake_ollama([text_reply("first"), text_reply("second")])

    cli.run(client=client)

    user_texts = [m["content"] for m in client.calls[1]["messages"] if m["role"] == "user"]
    assert user_texts == ["one", "two"]

def request_offering_always(title="Write 2 chars to 'a.txt'") -> ApprovalRequest:
    return ApprovalRequest(tool_call_id="1.1", tool_name="write_file", risk="write", title=title,
                           options=("allow_once", "allow_always", "reject_once"))


@pytest.mark.parametrize("answer", ["a", "A", "always"])
def test_a_allows_for_the_session_when_it_is_offered(monkeypatch, answer):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or answer)

    assert cli.TerminalApprover().decide(request_offering_always()) is Decision.ALLOW_ALWAYS
    assert prompts == ["\n[APPROVE] Write 2 chars to 'a.txt'? [y]es / [a]lways for this session / [n]o: "]


def test_y_is_allow_once_when_always_is_offered(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert cli.TerminalApprover().decide(request_offering_always()) is Decision.ALLOW_ONCE


@pytest.mark.parametrize("answer", ["n", "", "no"])
def test_anything_else_is_refused_when_always_is_offered(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)

    assert cli.TerminalApprover().decide(request_offering_always()) is Decision.REJECT_ONCE


def test_a_is_refused_when_always_is_not_offered(monkeypatch):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "a")

    assert cli.TerminalApprover().decide(request_for()) is Decision.REJECT_ONCE
    assert prompts == ["\n[APPROVE] Write 2 chars to 'a.txt'? (y/n): "]


def test_render_diff_preview_shows_the_changes_with_the_path(capsys):
    cli.render(ApprovalRequest(
        tool_call_id="1.1",
        tool_name="edit_file",
        risk="write",
        title="Edit 'a.txt' (replace one occurrence)",
        preview=Preview(text="-a\n+b\n", path="a.txt", kind="diff"),
    ))

    assert capsys.readouterr().out == "\n[PROPOSED CHANGES for 'a.txt']\n-a\n+b\n\n[END PROPOSED CHANGES]\n"


def test_render_diff_preview_without_a_path_is_a_plain_summary(capsys):
    cli.render(ApprovalRequest(
        tool_call_id="1.1",
        tool_name="git_commit",
        risk="write",
        title='Commit staged changes with message "m"',
        preview=Preview(text="Message:\nm", kind="diff"),
    ))

    assert capsys.readouterr().out == "\n[PROPOSED CHANGES]\nMessage:\nm\n[END PROPOSED CHANGES]\n"


def test_repl_a_allows_the_same_tool_for_the_rest_of_the_session(fake_ollama, monkeypatch, workspace, capsys):
    first, second = workspace / "a.txt", workspace / "b.txt"
    prompts = []
    answers = iter(["write one", "a", "write two", "exit"])

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(first), "content": "1"})),
        text_reply("one saved"),
        tool_reply(("write_file", {"path": str(second), "content": "2"})),
        text_reply("two saved"),
    ])

    cli.run(client=client)

    approvals = [p for p in prompts if "[APPROVE]" in p]
    assert len(approvals) == 1
    assert "[a]lways for this session" in approvals[0]
    assert first.read_text(encoding="utf-8") == "1"
    assert second.read_text(encoding="utf-8") == "2"
    assert "Agent: two saved" in capsys.readouterr().out


def test_eof_at_an_approval_refuses_the_tool(monkeypatch):
    def no_input(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)

    assert cli.TerminalApprover().decide(request_for()) is Decision.REJECT_ONCE


def test_eof_at_an_approval_refuses_the_tool_then_the_repl_exits(fake_ollama, monkeypatch, workspace, capsys):
    target = workspace / "a.txt"
    answers = iter(["write it"])  # then the input ends: the approval gets EOF, the next prompt gets EOF

    def fake_input(prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    client = fake_ollama([
        tool_reply(("write_file", {"path": str(target), "content": "hi"})),
        text_reply("I did not write it"),
    ])

    cli.run(client=client)  # no EOFError escapes

    assert not target.exists()
    assert client.calls[1]["messages"][-1] == {
        "role": "tool", "content": "User denied file write.", "tool_name": "write_file"
    }
    assert "Agent: I did not write it" in capsys.readouterr().out


def test_eof_at_the_prompt_exits_cleanly(fake_ollama, monkeypatch, capsys):
    def no_input(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)

    cli.run(client=fake_ollama([]))  # no EOFError escapes

    assert f"Local Agent ready ({DEFAULT_MODEL})" in capsys.readouterr().out


def test_piped_empty_stdin_exits_zero_without_a_traceback(tmp_path):
    """The source build, as the exe does: stdin ends at once, so the REPL ends with exit 0."""
    code = ("from local_agent.config import Settings\n"
            "from local_agent.frontends import cli\n"
            "cli.run(settings=Settings(plugins_enabled=False, host='http://127.0.0.1:1'))\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, input="", capture_output=True,
                            text=True, encoding="utf-8", timeout=120)

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert "Local Agent ready" in result.stdout
