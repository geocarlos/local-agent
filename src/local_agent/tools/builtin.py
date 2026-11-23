"""Built-in tools: read_file, write_file, run_command.

Moved unchanged from the old agent.py. The approval prompts still use
input()/print(); T02 replaces them with the Approver interface.
"""

import subprocess
from pathlib import Path

MAX_OUTPUT_CHARS = 8000      # Cap tool output so it can't overflow the model's context
COMMAND_TIMEOUT_S = 60       # Kill shell commands that hang


def _truncate(text: str) -> str:
    """Trim long tool output and say so, so the model knows it was cut."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n...[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


def read_file(path: str) -> str:
    """Read the contents of a file."""
    try:
        return _truncate(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return f"Error: {e}"


def write_file(path: str, content: str) -> str:
    """Write content to a file (asks for approval first)."""
    preview = content if len(content) <= 1500 else content[:1500] + f"\n...[{len(content) - 1500} more chars]"
    print(f"\n[PROPOSED CONTENT for '{path}']\n{preview}\n[END PROPOSED CONTENT]")
    confirm = input(f"\n[APPROVE] Write {len(content)} chars to '{path}'? (y/n): ").strip().lower()
    if confirm != "y":
        return "User denied file write."
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
    except Exception as e:
        return f"Error writing {path}: {e}"
    return f"Successfully wrote to {path}."


def run_command(command: str) -> str:
    """Run a shell command (asks for approval first)."""
    confirm = input(f"\n[APPROVE] Run command `{command}`? (y/n): ").strip().lower()
    if confirm != "y":
        return "User denied command execution."
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
