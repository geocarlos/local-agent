"""Read-tier tools: list_dir, glob, grep, git_status, git_diff, git_log.

All of them run without asking. Their path arguments are declared in `paths=`, so the
core resolves them against the workspace first (see local_agent.sandbox); a path
outside the workspace only gets here after its own approval. An optional path that is
not given means the workspace.

Searches never leave the folder they start from: glob and grep drop any match whose
resolved path (after following junctions and symlinks) is outside that folder, and
they do not descend into .git. Output is capped (item counts here, characters through
`truncate`). Git runs with cwd = workspace, no shell, a timeout, and no pager.
"""

import fnmatch
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from local_agent import sandbox
from local_agent.permissions import Risk
from local_agent.tools.output import truncate
from local_agent.tools.registry import ToolContext, tool

MAX_LIST_ENTRIES = 500           # entries list_dir shows
MAX_GLOB_RESULTS = 200           # paths glob returns
MAX_GREP_MATCHES = 100           # matching lines grep returns
MAX_GREP_LINE_CHARS = 300        # a matching line is cut to this many characters
MAX_GREP_FILE_BYTES = 2_000_000  # larger files are skipped by grep
BINARY_SNIFF_BYTES = 8192        # a NUL byte in this much of a file means binary
MAX_GIT_LOG = 200                # commits git_log returns at most
GIT_TIMEOUT_S = 30
SKIP_NAME = ".git"


def _workspace(context: ToolContext) -> Path:
    """The workspace with symlinks and junctions resolved, so relative paths compare correctly."""
    return Path(os.path.realpath(context.workspace))


def _base(path: str | None, context: ToolContext) -> Path:
    """The folder or file a path argument names, with links resolved. No path means the workspace."""
    return _workspace(context) if path is None else Path(os.path.realpath(path))


def _inside(path: str | Path, root: Path) -> bool:
    """True if `path` resolves to `root` or a place below it (junctions and symlinks followed)."""
    try:
        return sandbox.resolve(str(path), root).inside
    except sandbox.PathRefused:
        return False


def _show(path: Path, workspace: Path) -> str:
    """A path as the model sees it: relative to the workspace when inside it, otherwise absolute."""
    if _inside(path, workspace):
        return os.path.relpath(path, workspace)
    return str(path)


def _walk_files(root: Path) -> Iterator[Path]:
    """Files under `root` in sorted order, skipping .git and anything that resolves outside `root`."""
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != SKIP_NAME and _inside(Path(dirpath, d), root))
        for name in sorted(filenames):
            if name != SKIP_NAME and _inside(Path(dirpath, name), root):
                yield Path(dirpath, name)


@tool(risk=Risk.READ, paths=("path",))
def list_dir(
    path: Annotated[str | None, "Folder to list. Omit for the workspace."] = None,
    *,
    context: ToolContext,
) -> str:
    """List the entries of a folder, sorted by name. Folders are marked with a trailing '/'."""
    folder = _base(path, context)
    try:
        if not folder.is_dir():
            return f"Error: not a folder: {folder}"
        rows = [entry.name + ("/" if entry.is_dir() else "") for entry in folder.iterdir()]
    except OSError as e:
        return f"Error: {e}"
    if not rows:
        return "(empty folder)"
    rows.sort(key=str.casefold)
    shown = rows[:MAX_LIST_ENTRIES]
    if len(rows) > len(shown):
        shown.append(f"[... {len(rows) - len(shown)} more entries not shown]")
    return truncate("\n".join(shown), context.max_output_chars)


@tool(risk=Risk.READ, paths=("path",))
def glob(
    pattern: Annotated[str, "Glob pattern relative to the base folder, e.g. '**/*.py' or 'src/*.txt'. "
                            "No '..' and no absolute paths."],
    path: Annotated[str | None, "Base folder for the pattern. Omit for the workspace."] = None,
    *,
    context: ToolContext,
) -> str:
    """Find files and folders by glob pattern ('**' matches any depth). Results are workspace-relative,
    sorted, folders end with '/', and at most 200 are shown."""
    if not pattern or "\x00" in pattern or ":" in pattern or pattern[0] in "\\/" \
            or ".." in re.split(r"[\\/]", pattern):
        return ("Error: the pattern must be relative to the base folder, with no '..', no drive "
                "and no leading slash.")
    root = _base(path, context)
    workspace = _workspace(context)
    truncated = False
    found: list[Path] = []
    try:
        if not root.is_dir():
            return f"Error: not a folder: {root}"
        for match in root.glob(pattern):
            if SKIP_NAME in match.relative_to(root).parts or not _inside(match, root):
                continue
            if len(found) == MAX_GLOB_RESULTS:
                truncated = True
                break
            found.append(match)
        lines = [_show(m, workspace) + (os.sep if m.is_dir() else "")
                 for m in sorted(found, key=lambda p: str(p).casefold())]
    except (OSError, ValueError, NotImplementedError) as e:
        return f"Error: {e}"
    if not lines:
        return "No matches."
    if truncated:
        lines.append(f"[stopped at {MAX_GLOB_RESULTS} results; narrow the pattern or the base folder]")
    return truncate("\n".join(lines), context.max_output_chars)


@tool(risk=Risk.READ, paths=("path",))
def grep(
    pattern: Annotated[str, "Python regular expression to search for in file contents."],
    path: Annotated[str | None, "File or folder to search. Omit for the workspace."] = None,
    include: Annotated[str | None, "Only search files whose name matches this glob, e.g. '*.py'."] = None,
    ignore_case: Annotated[bool, "True to match regardless of letter case."] = False,
    *,
    context: ToolContext,
) -> str:
    """Search file contents with a regular expression. Each match is 'path:line: text'. Binary files,
    .git and files over 2 MB are skipped. At most 100 matches are shown."""
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return f"Error: invalid regular expression {pattern!r}: {e}"
    root = _base(path, context)
    workspace = _workspace(context)
    if not root.exists():
        return f"Error: no such file or folder: {root}"
    matches: list[str] = []
    truncated = False
    for file in _walk_files(root):
        if include and not fnmatch.fnmatch(file.name, include):
            continue
        try:
            if file.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            data = file.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            if not regex.search(line):
                continue
            if len(matches) == MAX_GREP_MATCHES:
                truncated = True
                break
            text = line.rstrip()[:MAX_GREP_LINE_CHARS]
            matches.append(f"{_show(file, workspace)}:{number}: {text}")
        if truncated:
            break
    if not matches:
        return "No matches."
    if truncated:
        matches.append(f"[stopped at {MAX_GREP_MATCHES} matches; narrow the pattern, path or include]")
    return truncate("\n".join(matches), context.max_output_chars)


FILTER_KEY_PATTERN = r"^filter\..*\.(clean|smudge|process)$"


def _repo_filter_overrides(workspace: Path, env: dict, safe: list[str]) -> tuple[dict[str, str], str | None]:
    """Environment entries that blank every clean/smudge/process filter defined in the repo's own config
    (local or worktree scope, including include.path) and set each such filter's `required` to false,
    so a missing filter program (e.g. Git LFS left behind by `git lfs install --local`) does not fail
    the command. System and global filters are left alone. The entries go in as GIT_CONFIG_KEY_n /
    GIT_CONFIG_VALUE_n, not `-c`, because `-c` splits at the first '=' and a filter name may contain one.
    Any entries already in `env` are kept and the new ones are appended after them.

    Returns (entries, error). If the listing fails (timeout, OSError, or an exit code other than 0 or 1)
    the error text is returned and git must not run. Not being a repository is not an error here: the
    real command then gives the usual message. Reading config runs nothing."""
    try:
        res = subprocess.run(
            ["git", *safe, "config", "-z", "--show-scope", "--name-only", "--get-regexp", FILTER_KEY_PATTERN],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        return {}, "Error: could not read the git config, so git was not run (timed out)."
    except OSError as e:
        return {}, f"Error: could not read the git config, so git was not run: {e}"
    err = res.stderr.strip()
    if res.returncode == 1:  # no matching keys
        return {}, None
    if res.returncode == 128 and "not a git repository" in err.lower():
        return {}, None
    if res.returncode != 0:
        return {}, (f"Error: could not read the git config, so git was not run "
                    f"(exit {res.returncode}): {err[:200]}")
    # Output with -z is "scope\0key\0" pairs. A key may contain dots, and a filter name may contain
    # any character except a newline, so split on NUL only.
    fields = res.stdout.split("\0")
    pairs = [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]
    overrides: dict[str, str] = {}  # key -> value, de-duplicated
    for scope, key in pairs:
        if scope not in ("local", "worktree"):
            continue
        overrides[key] = ""
        name = key[len("filter."):].rsplit(".", 1)[0]
        overrides[f"filter.{name}.required"] = "false"
    if not overrides:
        return {}, None
    try:
        start = max(0, int(env.get("GIT_CONFIG_COUNT", "0")))
    except ValueError:
        start = 0
    entries = {"GIT_CONFIG_COUNT": str(start + len(overrides))}
    for i, (key, value) in enumerate(overrides.items(), start=start):
        entries[f"GIT_CONFIG_KEY_{i}"] = key
        entries[f"GIT_CONFIG_VALUE_{i}"] = value
    return entries, None


def _run_git(args: list[str], context: ToolContext, empty: str) -> str:
    """Run git in the workspace. Returns its output, `empty` when there is none, or a message on failure. Never raises."""
    workspace = _workspace(context)
    if shutil.which("git") is None:
        return "Error: git is not installed or not on PATH."
    # The repo's own config must not run anything: fsmonitor, textconv, hooks and filter
    # drivers are commands that a cloned or untrusted .git/config or .gitattributes could set.
    # GIT_CEILING_DIRECTORIES stops git from finding a repository above the workspace.
    # GIT_CONFIG_NOSYSTEM is not set: Git for Windows sets core.autocrlf=true in the system
    # config, and ignoring it makes every CRLF checkout look modified.
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CEILING_DIRECTORIES": str(workspace.parent),
    }
    safe = [
        "--no-pager",
        "-c", "color.ui=never",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", f"core.hooksPath={os.devnull}",
    ]
    overrides, error = _repo_filter_overrides(workspace, env, safe)
    if error is not None:
        return error
    env.update(overrides)
    try:
        res = subprocess.run(
            ["git", *safe, *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Error: git timed out after {GIT_TIMEOUT_S}s."
    except OSError as e:
        return f"Error running git: {e}"
    if res.returncode != 0:
        err = res.stderr.strip()
        if "not a git repository" in err.lower():
            return f"The workspace is not a git repository: {workspace}"
        return f"Error: git {args[0]} failed (exit {res.returncode}): {err}"
    if not res.stdout.strip():
        return empty
    return truncate(res.stdout.rstrip("\n"), context.max_output_chars)


@tool(risk=Risk.READ)
def git_status(context: ToolContext) -> str:
    """Show the git status of the workspace: the branch and the changed files (short format)."""
    return _run_git(["status", "--short", "--branch"], context, "Nothing to show.")


@tool(risk=Risk.READ, paths=("path",))
def git_diff(
    path: Annotated[str | None, "Only show changes to this file or folder. Omit for all changes."] = None,
    staged: Annotated[bool, "True to show staged changes instead of unstaged ones."] = False,
    *,
    context: ToolContext,
) -> str:
    """Show uncommitted changes as a unified diff. Unstaged by default; staged=true shows what is staged.
    Untracked files do not appear until they are added."""
    args = ["diff", "--no-ext-diff", "--no-textconv"]
    if staged:
        args.append("--cached")
    if path is not None:
        args += ["--", path]
    return _run_git(args, context, "No differences.")


@tool(risk=Risk.READ)
def git_log(
    max_count: Annotated[int, f"How many commits to show, newest first (1 to {MAX_GIT_LOG})."] = 20,
    *,
    context: ToolContext,
) -> str:
    """Show recent commits, one per line (short hash, refs, subject), newest first."""
    if not 1 <= max_count <= MAX_GIT_LOG:
        return f"Error: max_count must be between 1 and {MAX_GIT_LOG}."
    return _run_git(["log", f"--max-count={max_count}", "--oneline", "--decorate"], context, "No commits.")
