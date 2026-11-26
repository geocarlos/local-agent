"""Run a child process with a time limit that also ends the processes it started.

subprocess.run with a timeout kills only the direct child. A grandchild that inherited the
pipes (cmd running ping, for example) then keeps communicate() waiting until it exits, so the
call can run far past its limit. Here the child starts in its own process group, and on a
timeout (or Ctrl+C) the whole tree is killed: `taskkill /T /F` on Windows, the process group
elsewhere. Output read after the kill is limited to KILL_WAIT_S.
"""

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

KILL_WAIT_S = 5  # how long to wait for the output after the tree is killed


@dataclass(frozen=True)
class RunResult:
    """What a finished or timed-out process gave back. returncode is None when the timeout killed it."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


def _own_group_options() -> dict:
    """Popen options that put the child in its own process group, so its tree can be ended as one."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """End `proc` and every process it started. Falls back to killing `proc` alone if the tree cannot be ended."""
    if os.name == "nt":
        try:
            done = subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=KILL_WAIT_S)
            if done.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass  # already gone


def _output_after_kill(proc: subprocess.Popen) -> tuple[str, str]:
    """What the killed process wrote. A process outside the tree may still hold the pipes: then nothing is read."""
    try:
        stdout, stderr = proc.communicate(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        return "", ""
    return stdout or "", stderr or ""


def run_process(
    argv: Sequence[str] | str,
    *,
    cwd: Path | str,
    timeout: float,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> RunResult:
    """Run one command with no shell in between, text output, and stdin closed (or fed `input_text`).

    Raises OSError if the program cannot be started. A run that exceeds `timeout` is killed with its
    children and returns timed_out=True. Ctrl+C kills the tree too, then re-raises.
    """
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_own_group_options(),
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        stdout, stderr = _output_after_kill(proc)
        return RunResult(returncode=None, stdout=stdout, stderr=stderr, timed_out=True)
    except BaseException:
        # Ctrl+C is not delivered to the child's own group, so end the tree here before it propagates.
        kill_tree(proc)
        raise
    return RunResult(returncode=proc.returncode, stdout=stdout, stderr=stderr, timed_out=False)
