"""run_command shell choice and run_tests resolution (explicit > configured > detected).

Real shells run only for a trivial command, and only where the shell is installed. run_tests never
runs a real test suite here: subprocess.run is replaced and the argv it would run is checked.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import text_reply, tool_reply

from local_agent import tools
from local_agent.config import Settings
from local_agent.core import loop
from local_agent.core.events import ApprovalRequest, Decision, ToolResult
from local_agent.tools import builtin, process
from local_agent.tools.registry import ToolContext

HAS_POWERSHELL = shutil.which("pwsh") is not None or shutil.which("powershell") is not None
requires_powershell = pytest.mark.skipif(not HAS_POWERSHELL, reason="no PowerShell on PATH")
requires_cmd = pytest.mark.skipif(os.name != "nt" or shutil.which("cmd") is None, reason="cmd is Windows only")


class Recording:
    def __init__(self, decision: Decision = Decision.ALLOW_ONCE):
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> Decision:
        self.requests.append(request)
        return self.decision


def run_loop(client, ws: Path, approver, settings: Settings | None = None):
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "go"})
    loop.run_turn(messages, client=client, settings=settings or Settings(workspace=ws), emit=events.append,
                  approver=approver)
    return messages, events


def result_of(events) -> ToolResult:
    return next(e for e in events if isinstance(e, ToolResult))


def argv_text(argv) -> str:
    """The command line as one string, whether subprocess got a list (POSIX form) or a string (Windows cmd)."""
    return argv if isinstance(argv, str) else " ".join(argv)


class FakeRun:
    """Stands in for subprocess.Popen: records the argv and kwargs and reports a successful run."""

    def __init__(self, stdout: str = "ok"):
        self.calls: list[dict] = []
        self.stdout = stdout

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        return FakeProcess(self.stdout)


class FakeProcess:
    pid = 0
    returncode = 0

    def __init__(self, stdout: str):
        self._stdout = stdout

    def communicate(self, input=None, timeout=None):
        return self._stdout, ""

    def kill(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(process.subprocess, "Popen", fake)
    return fake


# ---------------------------------------------------------------- run_command: the shell choice

def test_the_default_shell_is_cmd_and_the_preview_says_so(tmp_path):
    request = tools.TOOLS["run_command"].approval_request("1.1", {"command": "dir"}, ToolContext(workspace=tmp_path))

    assert request.title == "Run cmd command `dir`"
    assert request.preview.kind == "command"
    assert request.preview.shell == "cmd"
    assert request.preview.text == "dir"


def test_the_default_shell_runs_cmd_and_powershell_is_by_argument(tmp_path, fake_run):
    tools.TOOLS["run_command"].call({"command": "dir"}, ToolContext(workspace=tmp_path))
    tools.TOOLS["run_command"].call({"command": "dir", "shell": "powershell"}, ToolContext(workspace=tmp_path))

    assert "/d /c" in argv_text(fake_run.calls[0]["argv"])
    assert "/d /c" not in argv_text(fake_run.calls[1]["argv"])


def test_the_shell_parameter_and_the_tool_description_name_cmd_as_the_default():
    tool = tools.TOOLS["run_command"]

    assert "cmd (the default)" in tool.parameters["properties"]["shell"]["description"]
    assert 'shell is "cmd" (the default) or "powershell"' in tool.description


def test_cmd_is_shown_as_cmd(tmp_path):
    request = tools.TOOLS["run_command"].approval_request(
        "1.1", {"command": "dir", "shell": "cmd"}, ToolContext(workspace=tmp_path))

    assert request.title == "Run cmd command `dir`"
    assert request.preview.shell == "cmd"


def test_a_shell_outside_the_choices_is_refused_before_the_question(fake_ollama, tmp_path, fake_run):
    client = fake_ollama([
        tool_reply(("run_command", {"command": "ls", "shell": "bash"})),
        text_reply("ok"),
    ])
    approver = Recording()

    _, events = run_loop(client, tmp_path, approver)

    assert approver.requests == []
    assert fake_run.calls == []
    assert result_of(events).status == "failed"
    assert "must be one of ['powershell', 'cmd']" in result_of(events).content


def test_a_missing_powershell_is_refused_before_the_question(fake_ollama, tmp_path, monkeypatch, fake_run):
    monkeypatch.setattr(builtin, "_powershell_exe", lambda: None)
    client = fake_ollama([
        tool_reply(("run_command", {"command": "Get-Date", "shell": "powershell"})),
        text_reply("ok"),
    ])
    approver = Recording()

    _, events = run_loop(client, tmp_path, approver)

    assert approver.requests == []
    assert fake_run.calls == []
    assert "PowerShell is not installed" in result_of(events).content


def test_powershell_prefers_pwsh_then_windows_powershell(monkeypatch):
    found = {"pwsh": "C:/pwsh.exe", "powershell": "C:/powershell.exe"}
    monkeypatch.setattr(builtin, "shutil", SimpleNamespace(which=lambda name: found.get(name)))
    assert builtin._powershell_exe() == "C:/pwsh.exe"

    del found["pwsh"]
    assert builtin._powershell_exe() == "C:/powershell.exe"


def test_powershell_runs_with_no_profile_non_interactive_and_closed_stdin(tmp_path, monkeypatch, fake_run):
    monkeypatch.setattr(builtin, "_powershell_exe", lambda: "C:/ps/pwsh.exe")

    result = tools.TOOLS["run_command"].call({"command": "Write-Output hi", "shell": "powershell"},
                                             ToolContext(workspace=tmp_path))

    assert result.startswith("EXIT CODE: 0")
    call = fake_run.calls[0]
    assert call["argv"] == ["C:/ps/pwsh.exe", "-NoProfile", "-NonInteractive", "-Command", "Write-Output hi"]
    assert call["stdin"] == subprocess.DEVNULL
    assert call["cwd"] == tmp_path


@requires_powershell
def test_powershell_runs_a_trivial_command_and_reports_its_output(tmp_path):
    result = tools.TOOLS["run_command"].call({"command": "Write-Output 'hello from ps'", "shell": "powershell"},
                                             ToolContext(workspace=tmp_path))

    assert result.startswith("EXIT CODE: 0")
    assert "hello from ps" in result


@requires_powershell
def test_powershell_runs_in_the_workspace_and_reports_a_nonzero_exit(tmp_path):
    result = tools.TOOLS["run_command"].call({"command": "(Get-Location).Path; exit 3", "shell": "powershell"},
                                             ToolContext(workspace=tmp_path))

    assert "EXIT CODE: 3" in result
    assert os.path.normcase(str(tmp_path.resolve())) in os.path.normcase(result)


@requires_cmd
def test_cmd_runs_a_trivial_command_in_the_workspace(tmp_path):
    result = tools.TOOLS["run_command"].call({"command": "echo hello from cmd & cd", "shell": "cmd"},
                                             ToolContext(workspace=tmp_path))

    assert result.startswith("EXIT CODE: 0")
    assert "hello from cmd" in result
    assert os.path.normcase(str(tmp_path.resolve())) in os.path.normcase(result)


def test_cmd_is_started_as_comspec_with_autorun_off_and_the_command_in_quotes(tmp_path, monkeypatch, fake_run):
    # Only builtin's view of os is changed (name and COMSPEC), not the process's os module.
    fake_os = SimpleNamespace(name="nt", environ={"COMSPEC": r"C:\Windows\system32\cmd.exe"})
    monkeypatch.setattr(builtin, "os", fake_os)

    tools.TOOLS["run_command"].call({"command": "dir /b", "shell": "cmd"}, ToolContext(workspace=tmp_path))

    assert fake_run.calls[0]["argv"] == r'"C:\Windows\system32\cmd.exe" /d /c "dir /b"'


@requires_cmd
def test_a_timeout_kills_the_children_too_so_the_call_returns_in_time(tmp_path):
    # ping is a child of cmd and holds the pipes; killing only cmd used to wait for it (about 14 s here).
    started = time.monotonic()
    result = tools.TOOLS["run_command"].call({"command": "ping -n 15 127.0.0.1"},
                                             ToolContext(workspace=tmp_path, command_timeout_s=2))
    elapsed = time.monotonic() - started

    assert result == "Error: command timed out after 2s and was killed."
    assert elapsed < 6, elapsed


# ---------------------------------------------------------------- run_tests: resolution order

def ctx(ws: Path, test_command: str | None = None) -> ToolContext:
    return ToolContext(workspace=ws, test_command=test_command)


def test_an_explicit_command_beats_the_configured_one_and_the_detected_one(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    assert builtin.resolve_test_command("python -m pytest -x", ctx(tmp_path, "configured")) == "python -m pytest -x"


def test_a_blank_explicit_command_falls_through_to_the_next_source(tmp_path):
    assert builtin.resolve_test_command("   ", ctx(tmp_path, "configured")) == "configured"


def test_the_configured_command_beats_detection(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    assert builtin.resolve_test_command(None, ctx(tmp_path, "npm run check")) == "npm run check"


def test_pyproject_gives_uv_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    assert builtin.resolve_test_command(None, ctx(tmp_path)) == "uv run pytest -q"


def test_package_json_gives_npm_test(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    assert builtin.resolve_test_command(None, ctx(tmp_path)) == "npm test"


def test_pyproject_wins_over_package_json(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    assert builtin.resolve_test_command(None, ctx(tmp_path)) == "uv run pytest -q"


def test_nothing_to_resolve_gives_none(tmp_path):
    assert builtin.resolve_test_command(None, ctx(tmp_path)) is None


def test_no_test_command_is_refused_before_the_question(fake_ollama, tmp_path, fake_run):
    client = fake_ollama([
        tool_reply(("run_tests", {})),
        text_reply("ok"),
    ])
    approver = Recording()

    _, events = run_loop(client, tmp_path, approver)

    assert approver.requests == []
    assert fake_run.calls == []
    assert result_of(events).content.startswith("Error: no test command.")


# ---------------------------------------------------------------- run_tests: the same execution path

def test_run_tests_asks_with_the_resolved_command_and_runs_it_through_cmd(fake_ollama, tmp_path, fake_run):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("run_tests", {})),
        text_reply("tests ran"),
    ])
    approver = Recording()

    _, events = run_loop(client, tmp_path, approver)

    assert len(approver.requests) == 1
    request = approver.requests[0]
    assert request.tool_name == "run_tests"
    assert request.title == "Run tests `uv run pytest -q` (in cmd)"
    assert request.preview.text == "uv run pytest -q"
    assert request.preview.shell == "cmd"
    assert "uv run pytest -q" in argv_text(fake_run.calls[0]["argv"])
    assert result_of(events).content.startswith("EXIT CODE: 0")


def test_run_tests_uses_the_configured_command_from_settings(fake_ollama, tmp_path, fake_run):
    client = fake_ollama([
        tool_reply(("run_tests", {})),
        text_reply("ok"),
    ])
    approver = Recording()

    run_loop(client, tmp_path, approver, settings=Settings(workspace=tmp_path, test_command="my-tests --fast"))

    assert approver.requests[0].preview.text == "my-tests --fast"
    assert "my-tests --fast" in argv_text(fake_run.calls[0]["argv"])


def test_run_tests_with_an_explicit_command_ignores_the_configured_one(fake_ollama, tmp_path, fake_run):
    client = fake_ollama([
        tool_reply(("run_tests", {"command": "pytest tests/test_x.py"})),
        text_reply("ok"),
    ])
    approver = Recording()

    run_loop(client, tmp_path, approver, settings=Settings(workspace=tmp_path, test_command="my-tests"))

    assert approver.requests[0].preview.text == "pytest tests/test_x.py"
    assert "my-tests" not in argv_text(fake_run.calls[0]["argv"])


def test_run_tests_is_denied_without_running(fake_ollama, tmp_path, fake_run):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    client = fake_ollama([
        tool_reply(("run_tests", {})),
        text_reply("ok"),
    ])

    _, events = run_loop(client, tmp_path, Recording(Decision.REJECT_ONCE))

    assert fake_run.calls == []
    assert result_of(events).content == "User denied command execution."


def test_run_tests_is_an_exec_tool_that_offers_allow_always():
    tool = tools.TOOLS["run_tests"]

    assert tool.risk.value == "exec"
    assert tool.parameters["properties"]["command"]["type"] == "string"
    assert tool.approval_request("1.1", {}, ToolContext()).options == (
        "allow_once", "allow_always", "reject_once")
