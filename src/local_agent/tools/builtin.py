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
from local_agent.client_tools import ClientError, ClientTools
from local_agent.core.events import Preview
from local_agent.permissions import Risk
from local_agent.tools.edit_tools import (
    EditError,
    cap_preview,
    client_for,
    client_write_message,
    read_text_file,
    unified_diff,
)
from local_agent.tools.output import truncate
from local_agent.tools.process import run_process
from local_agent.tools.registry import ToolCancelled, ToolContext, tool

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


def _current_for_preview(path: str, context: ToolContext) -> str | None:
    """The file's text for the preview: the editor's when the client can read, else the disk's. None for a new
    file or a file that is not UTF-8 text (the preview then shows the proposed content)."""
    client = client_for(context, "read")
    if client is not None:
        try:
            return client.read_text_file(path)
        except ClientError:
            return None  # a new file in the editor's view, or one it cannot read: show the new content
    if Path(path).is_file():
        try:
            return read_text_file(path)
        except EditError:
            return None  # not text: show the new content instead
    return None


def _write_preview(path: str, content: str, context: ToolContext) -> Preview:
    """A diff when the file exists and is UTF-8 text; otherwise the proposed content (a new file)."""
    before = _current_for_preview(path, context)
    if before is None:
        return Preview(text=cap_preview(content), path=path, kind="text")
    if client_for(context, "read") is not None:
        # The editor keeps the file's own line endings, so both texts are compared with LF.
        return Preview(text=unified_diff(before.replace("\r\n", "\n"), content.replace("\r\n", "\n"), path),
                       path=path, kind="diff")
    # write_file writes in text mode, so "\n" becomes os.linesep (CRLF on Windows).
    return Preview(text=unified_diff(before, content.replace("\n", os.linesep), path), path=path, kind="diff")




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


def _execute_in_client(shell: str, command: str, context: ToolContext, client: ClientTools) -> str:
    """Run one command in the editor's terminal (T20). cmd is sent as the command line, which the client runs
    through the platform shell; PowerShell is started directly with its arguments. The output is one stream."""
    if shell == "cmd":
        program, args = command, []
    else:
        exe = _powershell_exe()
        if exe is None:
            return "Error: PowerShell is not installed (neither pwsh nor powershell is on PATH). Nothing was run."
        program, args = exe, ["-NoProfile", "-NonInteractive", "-Command", command]
    try:
        res = client.run_command(program, args, str(context.workspace), context.command_timeout_s)
    except ClientError as e:
        return f"Error running command in the editor: {e}"
    if res.timed_out:
        return f"Error: command timed out after {context.command_timeout_s}s and was killed."
    exit_text = "none (no exit code)" if res.exit_code is None else str(res.exit_code)
    note = "\n[the editor dropped the start of the output to stay within its limit]" if res.truncated else ""
    return truncate(f"EXIT CODE: {exit_text}\nOUTPUT (stdout and stderr together):\n{res.output}{note}",
                    context.max_output_chars)


def _execute(shell: str, command: str, context: ToolContext) -> str:
    """Run one command in the workspace with stdin closed and the command timeout. Returns the exit code and output.
    With a client that runs terminal commands (T20), the command runs in the editor's terminal instead."""
    client = client_for(context, "terminal")
    if client is not None:
        return _execute_in_client(shell, command, context, client)
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
    client = client_for(context, "read")
    try:  # with a client, the editor's text (unsaved changes included); else the disk's
        text = client.read_text_file(path) if client is not None else Path(path).read_text(encoding="utf-8")
    except ToolCancelled:  # a cancel while the editor was asked: the core ends the call as cancelled
        raise
    except Exception as e:  # a ClientError from the editor, or an OSError from the disk
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


def _allow_always(path: str, context: ToolContext, **_: object) -> bool:
    """Whether "allow for this session" is offered for this write (see sandbox.session_allowance_ok)."""
    return sandbox.session_allowance_ok(path, plugin_dir=context.plugin_dir)


@tool(
    risk=Risk.WRITE,
    denied_message="User denied file write.",
    approval_title=_write_title,
    preview=_write_preview,
    allow_always=_allow_always,
    paths=("path",),
)
def write_file(path: str, content: str, *, context: ToolContext) -> str:
    """Create or overwrite a file with the given content."""
    client = client_for(context, "write")
    if client is not None:  # the change goes into the editor (see the client_tools module)
        try:
            saved = client.write_text_file(path, content)
        except ClientError as e:
            return f"Error writing {path}: {e}"
        return client_write_message("Successfully wrote to", path, saved)
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
    shell: Annotated[ShellName, "Shell: cmd (the default) or powershell."] = DEFAULT_SHELL,
    *,
    context: ToolContext,
) -> str:
    """Run a command in the workspace folder and return its exit code and output. cmd is the default shell."""
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
    """Run the project's tests and return the exit code and output. Uses command if given, else the
    configured test_command, else detects the project (pyproject.toml or package.json)."""
    resolved = resolve_test_command(command, context)
    if resolved is None:
        return NO_TEST_COMMAND
    return _execute("cmd", resolved, context)
