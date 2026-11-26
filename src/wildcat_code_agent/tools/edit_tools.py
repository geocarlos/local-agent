"""Write-tier editing: edit_file (exact-match replace, approval shows a unified diff) and the diff helpers.

edit_file changes one exact occurrence of old_text and never guesses. Zero matches and
several matches are both errors. The precheck returns them before the user is asked,
so no approval is requested for an edit that cannot apply.

Line endings are kept. A file whose every line ends in CRLF is matched with LF, and the
result is written back with CRLF, so old_text and new_text may use either. A file with
no CRLF is matched as the model wrote it. A mixed file is matched exactly as it is. The
file is written as bytes, so nothing outside the replaced text changes. Only UTF-8 text
is edited; a file with NUL bytes or invalid UTF-8 is refused.

write_file uses the diff helpers here for its own approval preview.
"""

import difflib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from wildcat_code_agent import sandbox
from wildcat_code_agent.client_tools import NOT_FOUND, ClientError, ClientTools
from wildcat_code_agent.core.events import Preview
from wildcat_code_agent.permissions import Risk
from wildcat_code_agent.tools.registry import ToolContext, tool

MAX_PREVIEW_CHARS = 1500  # How much of a diff or proposed content the approval shows
MAX_LISTED_MATCHES = 5    # Line numbers of ambiguous matches named in the error


def cap_preview(text: str) -> str:
    """Cut a preview to MAX_PREVIEW_CHARS and say how much was left out."""
    if len(text) <= MAX_PREVIEW_CHARS:
        return text
    return text[:MAX_PREVIEW_CHARS] + f"\n...[{len(text) - MAX_PREVIEW_CHARS} more chars]"


NO_NEWLINE_MARK = "\\ No newline at end of file"


def _lines(text: str) -> list[str]:
    """Lines with their endings, split on LF only (as read_file does). A last line without LF keeps none."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def unified_diff(before: str, after: str, path: str) -> str:
    """A unified diff of two texts with three lines of context, labelled with the file name, capped for display.

    A line with no newline at the end of its file is followed by the "\\ No newline at end of file" mark,
    as git shows it, so a change to the last line is visible and no newline is invented.
    """
    diff = difflib.unified_diff(
        _lines(before), _lines(after),
        fromfile=f"{path} (current)", tofile=f"{path} (proposed)", lineterm="\n",
    )
    out = [line if line.endswith("\n") else f"{line}\n{NO_NEWLINE_MARK}\n" for line in diff]
    return cap_preview("".join(out)) or "(no changes)"


def client_write_message(done: str, path: str, saved: bool) -> str:
    """The result of a write made through the client, e.g. done="Successfully edited". A change the client did
    not save is said so, because the model would otherwise run the next command against the old file on disk."""
    if saved:
        return f"{done} {path}."
    return (f"{done} {path} in the editor. The change is not saved to disk yet: the file has unsaved changes "
            "in the editor, and the user must save it.")


class EditError(Exception):
    """A reason an edit or a read cannot be done. The message is the text returned to the model."""


def read_text_file(path: str) -> str:
    """The text of an existing UTF-8 file. Raises EditError for a missing, unreadable, binary or non-UTF-8 file."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        raise EditError(f"Error: no such file: '{path}'. edit_file changes an existing file; "
                        "use write_file to create one.") from None
    except OSError as e:
        raise EditError(f"Error: cannot read '{path}': {e}") from None
    if b"\x00" in data:
        raise EditError(f"Error: '{path}' looks binary (it contains NUL bytes). Only UTF-8 text files can be edited.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise EditError(f"Error: '{path}' is not UTF-8 text. Only UTF-8 text files can be edited.") from None


@dataclass(frozen=True)
class EditPlan:
    """The file's text before and after one edit. Nothing is written until the edit is approved and run."""

    before: str
    after: str


def _line_ending_form(
    text: str, old_text: str, new_text: str,
) -> tuple[str, str, str, Callable[[str], str]]:
    """(body, old, new, restore): the file text and the two replacement texts in the form used for matching.

    `restore` turns the edited body back into the file's own line endings.
    """
    crlf = text.count("\r\n")
    if crlf and crlf == text.count("\n"):  # every line ends in CRLF
        return (text.replace("\r\n", "\n"), old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n"),
                lambda s: s.replace("\n", "\r\n"))
    if crlf == 0:  # LF file: the model's text is matched as LF
        return text, old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n"), lambda s: s
    return text, old_text, new_text, lambda s: s  # mixed endings: matched as written, nothing rewritten


def _occurrences(body: str, old: str) -> list[int]:
    """Start index of every occurrence of `old`, overlapping ones included (so an ambiguous match is never missed)."""
    found: list[int] = []
    start = 0
    while True:
        index = body.find(old, start)
        if index < 0:
            return found
        found.append(index)
        start = index + 1


def _not_found(path: str, body: str, old: str) -> str:
    message = (f"Error: old_text was not found in '{path}'. It must match the file exactly, including spaces, "
               "indentation and line breaks. Read the file again and copy the lines exactly.")
    wanted = [line.strip() for line in old.strip("\n").split("\n")]
    if any(wanted):
        lines = [line.strip() for line in body.split("\n")]
        starts = [i for i in range(len(lines) - len(wanted) + 1) if lines[i:i + len(wanted)] == wanted]
        if starts:
            message += (f" A block that matches apart from spaces at the start and end of its lines starts at "
                        f"line {starts[0] + 1}; copy that block exactly, including its whitespace.")
    return message + " No change was made."


def current_text(path: str, context: ToolContext) -> str:
    """The file's text as the edit sees it: the editor's (unsaved changes included) when the client can read,
    else the disk's. Raises EditError with the reason when it cannot be read as UTF-8 text."""
    client = client_for(context, "read")
    if client is None:
        return read_text_file(path)
    try:
        text = client.read_text_file(path)
    except ClientError as e:
        if e.code == NOT_FOUND:
            raise EditError(f"Error: no such file: '{path}'. edit_file changes an existing file; "
                            "use write_file to create one.") from None
        raise EditError(f"Error: cannot read '{path}' in the editor: {e}") from None
    if "\x00" in text:
        raise EditError(f"Error: '{path}' looks binary (it contains NUL bytes). Only UTF-8 text files can be edited.")
    return text


def client_for(context: ToolContext | None, feature: str) -> ClientTools | None:
    """The client, if it advertised `feature` ("read", "write" or "terminal") for this call; else None (local)."""
    client = context.client if context is not None else None
    if client is None or not getattr(client, f"can_{feature}", False):
        return None
    return client


def plan_edit(path: str, old_text: str, new_text: str, text: str | None = None) -> EditPlan:
    """The edit as a plan. Raises EditError with the reason when it cannot apply exactly once.
    `text` is the file's current text; when None it is read from the disk."""
    if old_text == "":
        raise EditError("Error: old_text is empty. Give the exact text to replace. "
                        "To write a whole file, use write_file.")
    if old_text == new_text:
        raise EditError("Error: old_text and new_text are the same, so the edit would change nothing.")
    if text is None:
        text = read_text_file(path)
    body, old, new, restore = _line_ending_form(text, old_text, new_text)
    found = _occurrences(body, old)
    if not found:
        raise EditError(_not_found(path, body, old))
    if len(found) > 1:
        # Two matches on one line (overlapping, as in "aa" in "aaa") name that line once.
        numbers = list(dict.fromkeys(body.count("\n", 0, index) + 1 for index in found))
        shown = numbers[:MAX_LISTED_MATCHES]
        more = f" and {len(numbers) - len(shown)} more" if len(numbers) > len(shown) else ""
        noun = "line" if len(numbers) == 1 else "lines"
        listed = ", ".join(str(n) for n in shown)
        raise EditError(f"Error: old_text matches {len(found)} places in '{path}' (at {noun} {listed}{more}). "
                        "Include more of the surrounding lines in old_text so that it matches exactly once. "
                        "No change was made.")
    index = found[0]
    edited = body[:index] + new + body[index + len(old):]
    return EditPlan(before=text, after=restore(edited))


def _edit_title(path: str, old_text: str, new_text: str) -> str:
    return f"Edit '{path}' (replace one occurrence)"


def _edit_plan(path: str, old_text: str, new_text: str, context: ToolContext) -> EditPlan:
    """The plan against the file's current text (see current_text). Raises EditError."""
    return plan_edit(path, old_text, new_text, text=current_text(path, context))


def _edit_preview(path: str, old_text: str, new_text: str, context: ToolContext) -> Preview:
    try:
        plan = _edit_plan(path, old_text, new_text, context)
    except EditError as e:  # the precheck ran first, so this only happens if the file changed since
        return Preview(text=str(e), path=path, kind="text")
    return Preview(text=unified_diff(plan.before, plan.after, path), path=path, kind="diff")


def _edit_precheck(path: str, old_text: str, new_text: str, context: ToolContext) -> str | None:
    try:
        _edit_plan(path, old_text, new_text, context)
    except EditError as e:
        return str(e)
    return None


def _allow_always(path: str, context: ToolContext, **_: object) -> bool:
    """Whether "allow for this session" is offered for this edit (see sandbox.session_allowance_ok)."""
    return sandbox.session_allowance_ok(path, plugin_dir=context.plugin_dir)


@tool(
    risk=Risk.WRITE,
    denied_message="User denied file edit.",
    approval_title=_edit_title,
    preview=_edit_preview,
    precheck=_edit_precheck,
    allow_always=_allow_always,
    paths=("path",),
)
def edit_file(
    path: str,
    old_text: Annotated[str, "Exact text to replace; it must occur exactly once in the file."],
    new_text: Annotated[str, "Text that replaces old_text. An empty string deletes old_text."],
    *,
    context: ToolContext,
) -> str:
    """Replace one exact occurrence of old_text in an existing text file. Include enough surrounding lines
    to make old_text unique; if it is missing or occurs more than once, nothing changes."""
    try:
        plan = _edit_plan(path, old_text, new_text, context)
    except EditError as e:
        return str(e)
    client = client_for(context, "write")
    if client is not None:
        try:
            saved = client.write_text_file(path, plan.after)
        except ClientError as e:
            return f"Error writing {path}: {e}"
        return client_write_message("Successfully edited", path, saved)
    try:
        Path(path).write_bytes(plan.after.encode("utf-8"))
    except (OSError, UnicodeEncodeError) as e:
        return f"Error writing {path}: {e}"
    return f"Successfully edited {path}."
