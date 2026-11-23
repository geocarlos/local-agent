"""Built-in tools: read_file (read), write_file (write), run_command (exec).

Tools never print or read input. The core asks the Approver before write and
exec tools run, and the frontend renders the preview from the ApprovalRequest.
"""

import subprocess
from pathlib import Path

from local_agent.core.events import Preview
from local_agent.permissions import Risk
from local_agent.tools.registry import tool

MAX_OUTPUT_CHARS = 8000      # Cap tool output so it can't overflow the model's context
COMMAND_TIMEOUT_S = 60       # Kill shell commands that hang
MAX_PREVIEW_CHARS = 1500     # How much proposed file content the approval shows


def _truncate(text: str) -> str:
    """Trim long tool output and say so, so the model knows it was cut."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n...[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


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


@tool(risk=Risk.READ)
def read_file(path: str) -> str:
    """Read the contents of a file."""
    try:
        return _truncate(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return f"Error: {e}"


@tool(
    risk=Risk.WRITE,
    denied_message="User denied file write.",
    approval_title=_write_title,
    preview=_write_preview,
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
def run_command(command: str) -> str:
    """Run a shell command (asks for approval first)."""
    try:
        res = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {COMMAND_TIMEOUT_S}s and was killed."
    except Exception as e:
        return f"Error running command: {e}"
    return _truncate(f"EXIT CODE: {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
