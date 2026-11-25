"""Approval policy, session allowances and the Approver interface.

The core asks an Approver before any tool whose risk tier is not `read`. The
core never reads input itself; a frontend supplies the Approver (the CLI asks
on the terminal, a GUI shows a dialog).

A user can answer "allow always for this session" to a tool's own request. The
core records that in a SessionApprovals object that the frontend holds for the
whole session, so a new session starts empty. The allowance is scoped to one
tool name and one workspace. It never covers a path outside the workspace: that
request is asked every time (see sandbox.py). It is also not offered for a write under
a .git folder, and not for git_commit (each tool's `allow_always`).
"""

import os
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from local_agent.core.events import ApprovalRequest, Decision


class Risk(StrEnum):
    """Risk tier of a tool. Decides whether the user is asked first."""

    READ = "read"
    WRITE = "write"
    EXEC = "exec"
    NETWORK = "network"


def needs_approval(risk: Risk | str) -> bool:
    """Read tools run without asking. Write, exec and network always ask (unless allowed for the session)."""
    return Risk(risk) is not Risk.READ


class SessionApprovals:
    """Tools the user allowed "for this session", each for one workspace.

    The key is (tool name, workspace folder), with the folder resolved and normalised,
    so the same folder spelled two ways is one workspace. Nothing else is recorded:
    a tool allowed in one workspace still asks in another, and an outside-workspace
    request is never stored here.
    """

    def __init__(self) -> None:
        self._allowed: set[tuple[str, str]] = set()

    @staticmethod
    def _key(tool_name: str, workspace: Path | str) -> tuple[str, str]:
        return tool_name, os.path.normcase(os.path.realpath(workspace))

    def allows(self, tool_name: str, workspace: Path | str) -> bool:
        return self._key(tool_name, workspace) in self._allowed

    def allow(self, tool_name: str, workspace: Path | str) -> None:
        self._allowed.add(self._key(tool_name, workspace))


class Approver(Protocol):
    """Answers approval requests. Called synchronously from the agent loop."""

    def decide(self, request: ApprovalRequest) -> Decision: ...


class DenyAll:
    """Default Approver: refuses everything. Used when no frontend supplies one."""

    def decide(self, request: ApprovalRequest) -> Decision:
        return Decision.REJECT_ONCE
