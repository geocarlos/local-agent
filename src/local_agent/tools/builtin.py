"""Built-in tools: read_file (read), write_file (write), run_command (exec).

Tools never print or read input. The core asks the Approver before write and
exec tools run, and the frontend renders the preview from the ApprovalRequest.
Output limits, the command timeout and the workspace (the start folder of
run_command) come from the ToolContext the core passes in.

The path arguments of read_file and write_file are resolved and checked by the
core before they run (see local_agent.sandbox), so these functions get absolute
paths inside the workspace or an approved path outside it. run_command is not
sandboxed beyond its start folder: a shell command can name any path, so it
always asks first.
"""

import subprocess
from pathlib import Path
from typing import Annotated

from local_agent.core.events import Preview
from local_agent.permissions import Risk
from local_agent.tools.output import truncate
from local_agent.tools.registry import ToolContext, tool

MAX_PREVIEW_CHARS = 1500     # How much proposed file content the approval shows


def _write_title(path: str, content: str) -> str:
    return f"Write {len(content)} chars to '{path}'"


def _write_preview(path: str, content: str) -> Preview:
    text = content
    if len(content) > MAX_PREVIEW_CHARS:
        text = content[:MAX_PREVIEW_CHARS] + f"\n...[{len(content) - MAX_PREVIEW_CHARS} more chars]"
    return Preview(text=text, path=path, kind="text")


def _command_title(command: str) -> str:
    return f"Run command `{command}`"


def _command_preview(command: str) -> Preview:
    return Preview(text=command, kind="command")


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
)
def run_command(command: str, context: ToolContext) -> str:
    """Run a shell command (asks for approval first)."""
    try:
        res = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=context.command_timeout_s,
            cwd=context.workspace,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {context.command_timeout_s}s and was killed."
    except Exception as e:
        return f"Error running command: {e}"
    return truncate(f"EXIT CODE: {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
                     context.max_output_chars)
