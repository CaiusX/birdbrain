"""Durable little JSON state files (sync high-water marks).

A field unit's normal shutdown is a power cut — the net-watchdog reboots it, or
someone pulls the plug. These files are tiny and rewritten constantly, so the
window is small but it is hit, and what is on the other side of it matters: a
sync mark is the *only* thing standing between a reconnecting unit and either
re-sending its whole history or silently skipping its backlog.

So two properties, both of which the plain ``write_text`` this replaces lacked:

* **Writes are atomic.** Content lands in a temp file in the same directory,
  is fsynced, and is then ``os.replace``d over the target — a rename within a
  directory is atomic, so a reader sees either the old file or the new one,
  never a truncated one. The parent directory is fsynced too, which is the step
  people skip and the one that makes the rename itself survive the power cut.

* **Reads distinguish "missing" from "corrupt".** They are completely different
  situations — missing means first run, corrupt means we had a mark and lost it
  — and a caller that conflates them will happily re-seed and drop a backlog.
  The previous file is kept as ``<name>.bak`` so corruption is usually
  recoverable at the cost of a few duplicates rather than a lost queue.
"""
from __future__ import annotations

import contextlib
import json
import os
from enum import StrEnum
from pathlib import Path


class StateRead(StrEnum):
    """How :func:`read_json_state` obtained (or failed to obtain) the state."""

    OK = "ok"                # primary file parsed
    RECOVERED = "recovered"  # primary unreadable, .bak parsed instead
    MISSING = "missing"      # nothing on disk — genuine first run
    CORRUPT = "corrupt"      # primary unreadable and no usable .bak


def write_json_atomic(path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``path`` atomically, keeping the old copy.

    Never leaves a partial file: a crash before the ``os.replace`` leaves the
    previous content intact, and a crash after it leaves the new content whole.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    payload = json.dumps(data)

    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())

    # Keep the previous good copy before clobbering it. Best-effort: a missing
    # or unreadable original must not stop us writing the new one.
    if path.exists():
        with contextlib.suppress(OSError):
            os.replace(path, path.with_name(f"{path.name}.bak"))

    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_json_state(path: Path) -> tuple[dict | None, StateRead]:
    """Load ``path``, falling back to its ``.bak``.

    Returns ``(data, outcome)``. Callers must branch on the outcome —
    :attr:`StateRead.MISSING` and :attr:`StateRead.CORRUPT` mean opposite
    things and treating both as "start fresh" is how a backlog disappears.
    """
    path = Path(path)
    primary = _load(path)
    if primary is not None:
        return primary, StateRead.OK

    backup = _load(path.with_name(f"{path.name}.bak"))
    if backup is not None:
        # Only meaningful if the primary existed and was bad; if neither is
        # there we fall through to MISSING below.
        return backup, StateRead.RECOVERED

    if not path.exists():
        return None, StateRead.MISSING
    return None, StateRead.CORRUPT


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename inside it is durable.

    Unix-only in practice: Windows cannot open a directory as a file. Failing
    here must never break the write, which has already landed.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)
