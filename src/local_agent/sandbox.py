r"""Workspace sandbox: where a tool's path arguments point, and whether that is inside the workspace.

The core calls `check_arguments` for each tool that declares path arguments
(`paths=` in tools/registry.py), before the tool runs. Paths are resolved
against the workspace, never the process cwd, to an absolute path with `..`,
symlinks and junctions followed. A path inside the workspace runs without extra
approval. A path outside it needs its own approval (scope "outside_workspace"),
and is denied when there is no approver. Tools receive the resolved path.

Refused outright, without asking (`PathRefused`), on Windows:
- alternate data streams (`name:stream`, anything with a colon after the drive)
- a name ending in a dot or space: Windows drops them, so `jn.` opens the link `jn`
- device namespace paths (`\\.\...`)
- reserved device names (CON, NUL, COM1, LPT1, ...), in any folder or with an extension
- an empty path or one with a NUL character (on any OS)

Resolved as follows (checked on Windows 11 with Python 3.14, see TASKS.md T05):
- `..` and `.`: Win32 applies them lexically before following links, and
  `os.path.realpath` does the same, so the result matches what `open()` opens.
- junctions and symlinks: followed by `realpath` (junctions need no privilege; file
  symlinks may be refused by Windows, see the tests).
- 8.3 short names (`GEOCAR~1`) of existing folders: expanded to long names.
- drive-relative `C:foo`: relative to the workspace on the same drive; on another drive it is outside.
- `\foo` (no drive): on the workspace's drive.
- `\\?\C:\...` and `\\?\UNC\...`: the prefix is removed and the rest resolved.
- `\\.\...` (device namespace): refused outright.
- UNC `\\server\share\...`: outside unless it is the workspace's own share.
- Case and slashes: compared case-insensitively on Windows (`os.path.normcase`).
- The prefix trick (`C:\work-evil` for workspace `C:\work`) is outside: the check
  compares whole path components, not string prefixes.

Not handled: hard links (a hard link inside the workspace to an outside file looks
inside), and a path swapped for a link after the check (a race). Commands
(run_command) are not parsed at all; they always need approval and run in the workspace.
"""

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.core.events import ApprovalRequest

_NT = os.name == "nt"
_DRIVE = re.compile(r"^[A-Za-z]:")
_SEPARATORS = re.compile(r"[\\/]")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3])$", re.IGNORECASE)
_VERBATIM = "\\\\?\\"           # \\?\
_VERBATIM_UNC = "\\\\?\\UNC\\"  # \\?\UNC\
_DEVICE = "\\\\.\\"             # \\.\


class PathRefused(ValueError):
    """A path that is refused outright. It is never offered for approval."""


@dataclass(frozen=True)
class ResolvedPath:
    """One path argument after resolution."""

    requested: str  # what the model sent
    path: Path      # absolute and fully resolved: what the tool receives
    inside: bool    # True if `path` is the workspace or below it


def resolve(requested: object, workspace: Path) -> ResolvedPath:
    """Resolve one path argument against the workspace. Raises PathRefused for names that are never allowed."""
    if not isinstance(requested, str):
        raise PathRefused(f"a path must be text, got {requested!r}")
    if requested == "":
        raise PathRefused("the path is empty")
    if "\x00" in requested:
        raise PathRefused("the path contains a NUL character")
    if _NT:
        _check_windows_name(requested)
    root = _without_verbatim(os.path.realpath(workspace))
    resolved = _without_verbatim(os.path.realpath(os.path.join(root, _without_verbatim(requested))))
    return ResolvedPath(requested=requested, path=Path(resolved), inside=_is_inside(resolved, root))


def check_arguments(
    path_args: Sequence[str],
    arguments: Mapping[str, Any],
    workspace: Path,
) -> tuple[dict[str, Any], list[ResolvedPath]]:
    """Resolve the path arguments of one call.

    Returns the arguments with each path replaced by its resolved absolute text, and the
    paths that are outside the workspace. Raises PathRefused if any path is refused.
    """
    args = dict(arguments)
    outside: list[ResolvedPath] = []
    for name in path_args:
        if name not in args:
            continue
        item = resolve(args[name], workspace)
        args[name] = str(item.path)
        if not item.inside:
            outside.append(item)
    return args, outside


def outside_request(
    tool_call_id: str,
    tool_name: str,
    risk: str,
    item: ResolvedPath,
    workspace: Path,
) -> ApprovalRequest:
    """The separate approval asked for a path outside the workspace. The title gives the resolved path."""
    root = _without_verbatim(os.path.realpath(workspace))
    asked = "" if item.requested == str(item.path) else f" (requested as '{item.requested}')"
    title = f"Allow {tool_name} to access '{item.path}', which is outside the workspace '{root}'{asked}"
    return ApprovalRequest(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        risk=risk,
        title=title,
        scope="outside_workspace",
    )


def outside_denied_message(item: ResolvedPath, workspace: Path) -> str:
    """What the model receives when an outside path is not approved."""
    root = _without_verbatim(os.path.realpath(workspace))
    return (f"Access to '{item.path}' outside the workspace '{root}' was not approved. "
            "Use a path inside the workspace.")


def _check_windows_name(requested: str) -> None:
    if requested.startswith(_DEVICE):
        raise PathRefused("device namespace paths (\\\\.\\...) are not allowed")
    body = _without_verbatim(requested)
    if _DRIVE.match(body):
        body = body[2:]
    if ":" in body:
        raise PathRefused("alternate data streams (a colon in the name) are not allowed")
    for part in _SEPARATORS.split(body):
        if part in ("", ".", ".."):
            continue
        if part.endswith((".", " ")):
            raise PathRefused(f"'{part}' ends with a dot or space; Windows drops those, so the path may "
                              "mean another file")
        if _RESERVED.match(part.split(".", 1)[0]):
            raise PathRefused(f"'{part}' is a reserved Windows device name")


def _without_verbatim(text: str) -> str:
    """Drop a \\\\?\\ or \\\\?\\UNC\\ prefix (Windows only). Device paths (\\\\.\\) are left alone."""
    if not _NT or not text.startswith(_VERBATIM):
        return text
    if text[:len(_VERBATIM_UNC)].upper() == _VERBATIM_UNC:
        return "\\\\" + text[len(_VERBATIM_UNC):]
    return text[len(_VERBATIM):]


def _is_inside(path: str, root: str) -> bool:
    """True if `path` is `root` or below it, comparing whole path components case-insensitively."""
    p = os.path.normcase(path)
    r = os.path.normcase(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives, or a mix of absolute and relative paths
        return False
