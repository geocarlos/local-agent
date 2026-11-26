"""The client's file and terminal methods, as the core's tools see them (T20).

When the client (the VS Code extension) advertises `fs.readTextFile`, `fs.writeTextFile` or `terminal` in
`initialize`, the core's own read_file, write_file, edit_file, run_command and run_tests go through the client
for that session: reads see the editor's unsaved text, writes land in the editor, and commands run in the
editor's terminal. The approval, the sandbox and the workspace checks still happen in the core before any call.

This module holds only the seam: the protocol object a tool calls, and the errors. The JSON-RPC itself
(ids, waiting, cancel) is in frontends/server.py, which implements ClientTools for each session.
ACP v1 names: fs/read_text_file, fs/write_text_file, terminal/create, terminal/wait_for_exit, terminal/output,
terminal/kill and terminal/release (schema checked 2026-11-26; see docs/protocol.md).
"""

from dataclasses import dataclass
from typing import Protocol

# ACP's "resource not found" code. The client uses it for a file that does not exist.
NOT_FOUND = -32002


class ClientError(Exception):
    """The client answered with an error (`code`), or did not answer in time (`timed_out`)."""

    def __init__(self, message: str, *, code: int | None = None, timed_out: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.timed_out = timed_out


@dataclass(frozen=True)
class CommandResult:
    """What a command run in the client's terminal gave back. exit_code is None when it was killed or had no code."""

    exit_code: int | None
    output: str          # stdout and stderr together, as the terminal shows them
    truncated: bool      # the client dropped the start of the output to stay within its limit
    timed_out: bool      # the core's time limit ended the command


class ClientTools(Protocol):
    """What the core's tools may ask the client. A flag is False when the client did not advertise it."""

    can_read: bool
    can_write: bool
    can_terminal: bool

    def read_text_file(self, path: str) -> str:
        """The text of a file as the editor has it (unsaved changes included). Raises ClientError."""

    def write_text_file(self, path: str, content: str) -> bool:
        """Replace a file's text in the editor. Returns True if the client saved it to disk, False if the
        change is only in the editor (the user must save). Raises ClientError."""

    def run_command(self, command: str, args: list[str], cwd: str, timeout_s: float) -> CommandResult:
        """Run a command in the client's terminal and wait for it, at most timeout_s. Raises ClientError."""
