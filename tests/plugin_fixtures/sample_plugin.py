"""A sample plugin for the tests: one read tool and one write tool. Copied into a temp plugin folder by the tests."""

from pathlib import Path

from local_agent.plugin import Risk, tool


@tool(risk=Risk.READ)
def sample_count_words(text: str) -> str:
    """Count the words in a piece of text."""
    return f"{len(text.split())} words"


@tool(risk=Risk.WRITE, paths=("path",))
def sample_write_note(path: str, note: str) -> str:
    """Write a note to a file in the workspace."""
    Path(path).write_text(note, encoding="utf-8")
    return f"Wrote {path}."
