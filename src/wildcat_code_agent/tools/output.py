"""Shared output helpers for tools: cap long text and say so, and make text from outside safe to show.

`printable` is for text that is shown in the terminal (warnings, approval titles and previews). It turns
control characters into visible escapes, so a server cannot move the cursor, clear the screen or add a
line to the approval prompt. `without_controls` is for text sent to the model (descriptions): the
control characters become spaces.
"""

import re

# C0 and C1 controls, plus the Unicode line separators and bidi overrides that can reorder what is shown.
# Multi-line text keeps \t and \n; single-line fields escape them too.
_MULTI_LINE_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")
_SINGLE_LINE_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")
_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def truncate(text: str, limit: int) -> str:
    """Trim `text` to `limit` characters and append a marker that says how much was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def printable(text: str, *, single_line: bool = False) -> str:
    """`text` with control characters shown as escapes, for example "\\x1b" or "\\n", so it is safe to print.

    With `single_line=True` newlines and tabs are escaped too. Otherwise they are kept.
    """
    pattern = _SINGLE_LINE_UNSAFE if single_line else _MULTI_LINE_UNSAFE
    return pattern.sub(_escape, text)


def without_controls(text: str) -> str:
    """`text` with each control character replaced by a space (for text the model reads, not the terminal)."""
    return _SINGLE_LINE_UNSAFE.sub(" ", text)


def _escape(match: re.Match[str]) -> str:
    char = match.group()
    if char in _ESCAPES:
        return _ESCAPES[char]
    code = ord(char)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"
