"""Approval policy and the Approver interface.

The core asks an Approver before any tool whose risk tier is not `read`. The
core never reads input itself; a frontend supplies the Approver (the CLI asks
y/n on the terminal, a GUI shows a dialog).
"""

from enum import StrEnum
from typing import Protocol

from local_agent.core.events import ApprovalRequest, Decision


class Risk(StrEnum):
    """Risk tier of a tool. Decides whether the user is asked first."""

    READ = "read"
    WRITE = "write"
    EXEC = "exec"
    NETWORK = "network"


def needs_approval(risk: Risk | str) -> bool:
    """Read tools run without asking. Write, exec and network always ask."""
    return Risk(risk) is not Risk.READ


class Approver(Protocol):
    """Answers approval requests. Called synchronously from the agent loop."""

    def decide(self, request: ApprovalRequest) -> Decision: ...


class DenyAll:
    """Default Approver: refuses everything. Used when no frontend supplies one."""

    def decide(self, request: ApprovalRequest) -> Decision:
        return Decision.REJECT_ONCE
