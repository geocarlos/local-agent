"""Built-in tools: read_file (read), write_file (write), run_command and run_tests (exec).

Tools never print or read input. The core asks the Approver before write and
exec tools run, and the frontend renders the preview from the ApprovalRequest.
Output limits, the command timeout and the workspace (the start folder of
commands) come from the ToolContext the core passes in.

The path arguments of read_file and write_file are resolved and checked by the
core before they run (see local_agent.sandbox), so these functions get absolute
paths inside the workspace or an approved path outside it. Commands are not
sandboxed beyond their start folder: a shell command can name any path, so it
always asks first.

run_command runs cmd by default, or PowerShell (pwsh if installed, else Windows PowerShell)
when shell="powershell". run_tests runs the project's test command through cmd, which finds
npm.cmd without PowerShell's script execution policy getting in the way. Both use
tools/process.py, so a timeout ends the command's child processes as well.
"""

import os
import shutil
from pathlib import Path
from typing import Annotated, Literal

from local_agent import sandbox
from local_agent.core.events import Preview
from local_agent.permissions import Risk
from local_agent.tools.edit_tools import EditError, cap_preview, read_text_file, unified_diff
from local_agent.tools.output import truncate
from local_agent.tools.process import run_process
from local_agent.tools.registry import ToolContext, tool

ShellName = Literal["powershell", "cmd"]
SHELL_LABELS: dict[str, str] = {"powershell": "PowerShell", "cmd": "cmd"}
DEFAULT_SHELL = "cmd"
TEST_COMMAND_PYPROJECT = "uv run pytest -q"  # used when the workspace has a pyproject.toml
TEST_COMMAND_NPM = "npm test"                # used when the workspace has a package.json
NO_TEST_COMMAND = ("Error: no test command. Pass command, set test_command in config.toml, or add a "
                   "pyproject.toml (runs 'uv run pytest -q') or a package.json (runs 'npm test') to the "
                   "workspace. Nothing was run.")


def _write_title(path: str, content: str) -> str:
    return f"Write {len(content)} chars to '{path}'"


def _write_preview(path: str, content: str) -> Preview:
    """A diff when the file exists and is UTF-8 text; otherwise the proposed content (a new file)."""
    if Path(path).is_file():
        try:
            before = read_text_file(path)
        except EditError:
            before = None  # not text: show the new content instead
        if before is not None:
            # write_file writes in text mode, so "\n" becomes os.linesep (CRLF on Windows).
            after = content.replace("\n", os.linesep)
            return Preview(text=unified_diff(before, after, path), path=path, kind="diff")
    return Preview(text=cap_preview(content), path=path, kind="text")


def _shell(shell: str | None) -> str:
    return DEFAULT_SHELL if shell is None else shell


def _command_title(command: str, shell: str | None = None) -> str:
    return f"Run {SHELL_LABELS[_shell(shell)]} command `{command}`"


def _command_preview(command: str, shell: str | None = None) -> Preview:
    return Preview(text=command, kind="command", shell=SHELL_LABELS[_shell(shell)])


def _powershell_exe() -> str | None:
    """pwsh (PowerShell 7) if it is on PATH, else Windows PowerShell (powershell.exe)."""
    return shutil.which("pwsh") or shutil.which("powershell")


def _shell_argv(shell: str, command: str) -> list[str] | str | None:
    """The command line for a shell, with no shell in between. None if the shell is not installed.

    cmd on Windows is given as one string, as subprocess does for shell=True (COMSPEC /c "command"),
    because a list would quote the command's own quote characters in a way cmd does not read.
    """
    if shell == "cmd":
        if os.name != "nt":
            return ["cmd", "/d", "/c", command]
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return f'"{comspec}" /d /c "{command}"'
    exe = _powershell_exe()
    if exe is None:
        return None
    return [exe, "-NoProfile", "-NonInteractive", "-Command", command]


def _shell_precheck(command: str, shell: str | None = None) -> str | None:
    if _shell(shell) == "powershell" and _powershell_exe() is None:
        return "Error: PowerShell is not installed (neither pwsh nor powershell is on PATH). Nothing was run."
    return None


def _execute(shell: str, command: str, context: ToolContext) -> str:
    """Run one command in the workspace with stdin closed and the command timeout. Returns the exit code and output."""
    argv = _shell_argv(shell, command)
    if argv is None:
        return f"Error: {SHELL_LABELS[shell]} is not installed. Nothing was run."
    try:
        res = run_process(argv, cwd=context.workspace, timeout=context.command_timeout_s)
    except Exception as e:
        return f"Error running command: {e}"
    if res.timed_out:
        return f"Error: command timed out after {context.command_timeout_s}s and was killed."
    return truncate(f"EXIT CODE: {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
                    context.max_output_chars)


@tool(risk=Risk.READ, paths=("path",))
def read_file(
    path: str,
    start_line: Annotated[int | None, "First line to return, 1-based. Omit to start at line 1."] = None,
    end_line: Annotated[int | None, "Last line to return, inclusive. Omit to read to the end."] = None,
    *,
    context: ToolContext,
) -> str:
    """Read a text file: all of it, or only lines start_line..end_line (1-based, inclusive), numbered."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return f"Error: {e}"
    if start_line is None and end_line is None:
        return truncate(text, context.max_output_chars)
    # Split on "\n" only: str.splitlines() also breaks on \x0b, \x0c, \x1c-\x1e, \x85, U+2028 and U+2029,
    # which would number lines differently from the editor and from grep.
    lines = text[:-1].split("\n") if text.endswith("\n") else text.split("\n")
    if text == "":
        lines = []
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    total = len(lines)
    first = 1 if start_line is None else start_line
    last = total if end_line is None else end_line
    if first < 1 or (end_line is not None and last < first):
        return f"Error: bad line range {first}-{last}; lines are 1-based and end_line must be >= start_line."
    if total == 0:
        return "[lines 0-0 of 0: the file is empty]"
    if first > total:
        return f"Error: start_line {first} is past the end of the file ({total} lines)."
    last = min(last, total)
    body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(first, last + 1))
    return truncate(f"[lines {first}-{last} of {total}]\n{body}", context.max_output_chars)


@tool(
    risk=Risk.WRITE,
    denied_message="User denied file write.",
    approval_title=_write_title,
    preview=_write_preview,
    allow_always=sandbox.session_allowance_ok,
    paths=("path",),
)
def write_file(path: str, content: str) -> str:
    """Write content to a file (asks for approval first)."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
    except Exception as e:
        return f"Error writing {path}: {e}"
    return f"Successfully wrote to {path}."


@tool(
    risk=Risk.EXEC,
    denied_message="User denied command execution.",
    approval_title=_command_title,
    preview=_command_preview,
    precheck=_shell_precheck,
)
def run_command(
    command: str,
    shell: Annotated[ShellName,
                     "cmd (the default) runs the Windows command prompt; powershell runs PowerShell."] = DEFAULT_SHELL,
    *,
    context: ToolContext,
) -> str:
    """Run a command in the workspace folder and return its exit code and output (asks for approval first). shell is "cmd" (the default) or "powershell"."""
    return _execute(_shell(shell), command, context)


def resolve_test_command(command: str | None, context: ToolContext) -> str | None:
    """The test command to run, in this order: the one given, the configured test_command, then detection
    (pyproject.toml gives 'uv run pytest -q', package.json gives 'npm test'). None if none applies."""
    if command is not None and command.strip():
        return command.strip()
    if context.test_command:
        return context.test_command
    if (context.workspace / "pyproject.toml").is_file():
        return TEST_COMMAND_PYPROJECT
    if (context.workspace / "package.json").is_file():
        return TEST_COMMAND_NPM
    return None


def _tests_title(command: str | None = None, *, context: ToolContext) -> str:
    return f"Run tests `{resolve_test_command(command, context) or '(none)'}` (in cmd)"


def _tests_preview(command: str | None = None, *, context: ToolContext) -> Preview:
    return Preview(text=resolve_test_command(command, context) or "(none)", kind="command", shell="cmd")


def _tests_precheck(command: str | None = None, *, context: ToolContext) -> str | None:
    if resolve_test_command(command, context) is None:
        return NO_TEST_COMMAND
    return None


@tool(
    risk=Risk.EXEC,
    denied_message="User denied command execution.",
    approval_title=_tests_title,
    preview=_tests_preview,
    precheck=_tests_precheck,
)
def run_tests(
    command: Annotated[str | None, "Test command to run. Omit to use the project's test command."] = None,
    *,
    context: ToolContext,
) -> str:
    """Run the project's tests in the workspace and return the exit code and output (asks for approval first).
    Uses command if given, else the configured test_command, else 'uv run pytest -q' when there is a
    pyproject.toml, else 'npm test' when there is a package.json."""
    resolved = resolve_test_command(command, context)
    if resolved is None:
        return NO_TEST_COMMAND
    return _execute("cmd", resolved, context)
