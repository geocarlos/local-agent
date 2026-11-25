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

from local_agent.core.events import Preview
from local_agent.permissions import Risk
from local_agent.tools.registry import ToolContext, tool

MAX_PREVIEW_CHARS = 1500     # How much proposed file content the approval shows


def _truncate(text: str, limit: int) -> str:
    """Trim long tool output to `limit` characters and say so, so the model knows it was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


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
def read_file(path: str, context: ToolContext) -> str:
    """Read the contents of a file."""
    try:
        return _truncate(Path(path).read_text(encoding="utf-8"), context.max_output_chars)
    except Exception as e:
        return f"Error: {e}"


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
    return _truncate(f"EXIT CODE: {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
                     context.max_output_chars)
