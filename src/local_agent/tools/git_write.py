"""Write-tier git tool: git_commit.

Without paths, git_commit commits what is already staged; it never stages on its own.
With paths, it stages exactly those files (`git add --`) and commits only them
(`git commit --only`), so other staged changes stay out of the commit. The paths are
sandboxed like any file argument: a path outside the workspace gets its own question first.

Repository code does not run: git_commit and its `git add` use the read tools' hardening
(read_tools.hardened_git). Hooks are off (core.hooksPath), fsmonitor and untracked cache are
off, clean, smudge and process filters defined in the repository are blanked, and the
commit gets --no-verify and commit.gpgsign=false, so no pre-commit hook, filter or gpg
program runs. The listing in the approval preview uses the same hardened git.
Git still reads the system and global config, which is not repository-controlled.

With paths, a path whose filter attribute names a repo-defined filter (or any path that is a
folder or a wildcard, while the repo defines any filter) is refused before the question. Running
that filter would need the repo's own config, and blanking it would store the raw file instead of
what `git add` stores. The user stages such files with `git add` and calls git_commit without paths.

The commit is never remembered for the session: every git_commit asks (allow_always=False).

The message is passed on stdin (`commit -F -`), so it is never parsed as arguments.
A missing committer name or email, an empty message and nothing to commit are all
refused before the user is asked. If the commit fails after `git add`, the paths stay staged
and the error says so.
"""

import shutil
from pathlib import Path
from typing import Annotated

from local_agent.core.events import Preview
from local_agent.permissions import Risk
from local_agent.tools.edit_tools import cap_preview
from local_agent.tools.output import truncate
from local_agent.tools.process import run_process
from local_agent.tools.read_tools import _run_git, _workspace, hardened_git, repo_filter_names
from local_agent.tools.registry import ToolContext, tool

HOOKS_NOTE = ("Repository hooks and filters do not run, and the commit is not signed. "
              "Paths that use a repo-defined filter are refused: stage them yourself and commit without paths.")
GIT_NO_SIGNING = ["-c", "commit.gpgsign=false"]


def _paths(paths: list[str] | str | None) -> list[str]:
    """The paths as a list. A single string (a one-path call) is wrapped; None or an empty list means no paths."""
    if paths is None:
        return []
    if isinstance(paths, str):
        return [paths]
    return list(paths)


def _problem(output: str) -> str | None:
    """`output` if a read-tool helper returned an error message instead of git output, else None."""
    if output.startswith("Error") or output.startswith("The workspace is not a git repository"):
        return output
    return None


def _listing(items: list[str], context: ToolContext) -> str:
    """What the commit would record, for the preview: the given paths' status, or the staged diffstat."""
    if items:
        return _run_git(["status", "--short", "--", *items], context, "(no changes in these paths)")
    return _run_git(["diff", "--cached", "--stat"], context, "(nothing is staged)")


FILTER_REFUSAL = ("These paths use a repo-defined filter ({names}): {paths}. "
                  "Stage them yourself with `git add`, then call git_commit without paths.")


def _filter_refusal(names: list[str], paths: list[str]) -> str:
    return "Error: " + FILTER_REFUSAL.format(names=", ".join(names), paths=", ".join(paths)) + " Nothing was committed."


def _shown(item: str, workspace: Path) -> str:
    """A path for the message: relative to the workspace, with forward slashes, when it is inside it.
    The sandbox may have made it absolute already."""
    try:
        return Path(item).relative_to(workspace).as_posix()
    except ValueError:
        return item


def _is_plain_file(item: str, workspace: Path) -> bool:
    """True for one file name: no pathspec magic or wildcard, no backslash, and not a folder.
    Anything else is checked as a whole when the repo defines a filter."""
    if not item or item.startswith(":") or any(c in item for c in "*?[]\\"):
        return False
    return not (workspace / item).is_dir()


def _repo_filter_refusal(items: list[str], context: ToolContext) -> str | None:
    """Refuse paths mode when a path would run a repo-defined filter. Fails closed: if the repo config or
    the attributes cannot be read, the commit is refused. Reading config and attributes runs nothing."""
    workspace = _workspace(context)
    names, error = repo_filter_names(workspace)
    if error is not None:
        return f"{error} Nothing was committed."
    if not names:
        return None
    shown = [_shown(item, workspace) for item in items]  # the sandbox may have made them absolute
    plain = [item for item in shown if not _is_plain_file(item, workspace)]
    if plain:  # folders and wildcards can reach files with any filter, so the whole list is refused
        return _filter_refusal(names, plain)
    options, env, error = hardened_git(workspace)
    if error is not None:
        return f"{error} Nothing was committed."
    try:
        res = run_process(["git", *options, "check-attr", "-z", "filter", "--", *shown], cwd=workspace,
                          timeout=context.command_timeout_s, env=env)
    except OSError as e:
        return f"Error: could not read the filter attributes ({e}). Nothing was committed."
    if res.timed_out:
        return "Error: could not read the filter attributes (timed out). Nothing was committed."
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        return f"Error: could not read the filter attributes (exit {res.returncode}: {detail}). Nothing was committed."
    # Output with -z is one "path\0attribute\0value\0" triple per path, in the order given (one attribute).
    fields = res.stdout.split("\0")
    used: dict[str, str] = {}  # path -> the repo filter it names
    for item, i in zip(shown, range(0, len(fields) - 2, 3)):
        value = fields[i + 2]
        if value in names:
            used[item] = value
    if used:
        return _filter_refusal(sorted(set(used.values())), list(used))
    return None


def _commit_title(message: str, paths: list[str] | str | None = None) -> str:
    items = _paths(paths)
    scope = f"{len(items)} path(s)" if items else "staged changes"
    subject = message.strip().split("\n")[0] if message.strip() else ""
    return f'Commit {scope} with message "{subject}"'


def _commit_preview(message: str, paths: list[str] | str | None = None, *, context: ToolContext) -> Preview:
    text = (f"Message:\n{message.strip()}\n\nWill commit:\n{_listing(_paths(paths), context)}\n\n{HOOKS_NOTE}")
    return Preview(text=cap_preview(text), kind="diff")


def _commit_precheck(message: str, paths: list[str] | str | None = None, *, context: ToolContext) -> str | None:
    if not message.strip():
        return "Error: the commit message is empty. Nothing was committed."
    items = _paths(paths)
    inside = _run_git(["rev-parse", "--is-inside-work-tree"], context, "")
    if (problem := _problem(inside)) is not None:
        return problem
    # `git var` fails when git has no name or email for the committer (user.name, user.email, or the env vars).
    ident = _run_git(["var", "GIT_COMMITTER_IDENT"], context, "")
    if ident.startswith("Error"):
        return ("Error: git has no committer name or email (set user.name and user.email with git config). "
                f"Nothing was committed. ({ident})")
    if items:
        listing = _run_git(["status", "--short", "--", *items], context, "")
        if (problem := _problem(listing)) is not None:
            return problem
        if not listing:
            return "Error: none of the given paths has changes to commit. Nothing was committed."
        if (refusal := _repo_filter_refusal(items, context)) is not None:
            return refusal
    else:
        staged = _run_git(["diff", "--cached", "--name-only"], context, "")
        if (problem := _problem(staged)) is not None:
            return problem
        if not staged:
            return ("Error: nothing is staged to commit. Stage files with git add, or pass paths to commit them. "
                    "Nothing was committed.")
    return None


def _git_process(args: list[str], context: ToolContext, stdin_text: str | None) -> tuple[bool, str]:
    """Run git for a commit or its `add`, hardened and unsigned (see the module note). Returns (succeeded, output or error text)."""
    workspace = _workspace(context)
    if shutil.which("git") is None:
        return False, "git is not installed or not on PATH."
    options, env, error = hardened_git(workspace)
    if error is not None:
        return False, error.removeprefix("Error: ")
    try:
        res = run_process(["git", *options, *GIT_NO_SIGNING, *args], cwd=workspace,
                          timeout=context.command_timeout_s, env=env, input_text=stdin_text)
    except OSError as e:
        return False, f"error running git: {e}"
    if res.timed_out:
        return False, f"git timed out after {context.command_timeout_s}s; check git status before retrying"
    if res.returncode != 0:
        return False, f"exit {res.returncode}: {(res.stderr or res.stdout).strip()}"
    return True, res.stdout.strip()


@tool(
    risk=Risk.WRITE,
    denied_message="User denied git commit.",
    approval_title=_commit_title,
    preview=_commit_preview,
    precheck=_commit_precheck,
    allow_always=False,
    paths=("paths",),
)
def git_commit(
    message: Annotated[str, "Commit message. The first line is the subject."],
    paths: Annotated[list[str] | None,
                     "Files to stage and commit, and nothing else. Omit to commit what is already staged."] = None,
    *,
    context: ToolContext,
) -> str:
    """Commit the staged changes, or only the given paths (staged first). Paths that use a repo-defined
    filter are refused: stage those with git add, then commit without paths."""
    items = _paths(paths)
    if items:
        ok, output = _git_process(["add", "--", *items], context, None)
        if not ok:
            return f"Error: git add failed: {output}. Nothing was committed."
        ok, output = _git_process(["commit", "--no-verify", "--only", "-F", "-", "--", *items], context, message)
    else:
        ok, output = _git_process(["commit", "--no-verify", "-F", "-"], context, message)
    if not ok:
        still = " The paths are still staged." if items else ""
        return f"Error: git commit failed: {output}.{still}"
    return truncate(output, context.max_output_chars) if output else "Committed."
