"""Atomic file writes (T17): a crash leaves the old file or the new one, never half of a new one.

The text goes to a temp file beside the target, is flushed to disk, and then os.replace() moves it over
the target (one rename on the same volume). An optional `check` runs on the temp file first; if it raises,
the target is left alone and the temp file is removed.

On Windows os.replace() fails with PermissionError while another handle has the target open (another
connection reading the same session, an antivirus scan, the indexer). The rename is retried for a short
time (REPLACE_TRIES attempts, REPLACE_BACKOFF_S apart) before the error is raised.
"""

import contextlib
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

REPLACE_TRIES = 10
REPLACE_BACKOFF_S = 0.05


def _replace(source: Path, target: Path) -> None:
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_BACKOFF_S)


def atomic_write_text(path: Path, text: str, *, check: Callable[[Path], None] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if check is not None:
            check(temp)
        _replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
