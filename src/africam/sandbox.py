"""Cross-process sandbox feed for sources under test.

A source flagged ``sandbox:<name>`` runs real BirdNET detection so the operator
can verify capture works (e.g. by playing bird sounds), but its detections are
deliberately NOT persisted to the main ``detections`` table — they go here
instead, so the test never pollutes the live data.

The pipeline (writer) and web server (reader) are SEPARATE processes, so an
in-memory buffer can't be shared between them. We use a small JSON file per
source under ``data/sandbox/`` as the channel: the worker appends with an atomic
replace (single writer), the web endpoint reads. Nothing touches the database.
Bounded per source; cleared on go-live.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

_DIR = Path("data/sandbox")
_MAX_PER_SOURCE = 100
_lock = threading.Lock()


def _path(source_name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in source_name)
    return _DIR / f"{safe}.json"


def record(
    source_name: str,
    *,
    ts: str,
    common_name: str,
    scientific_name: str,
    confidence: float,
) -> None:
    """Append one sandbox detection to the source's feed file (atomic)."""
    entry = {
        "ts": ts,
        "common_name": common_name,
        "scientific_name": scientific_name,
        "confidence": round(float(confidence), 3),
    }
    with _lock:
        items = recent(source_name, limit=_MAX_PER_SOURCE)
        items.insert(0, entry)
        items = items[:_MAX_PER_SOURCE]
        _DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_DIR), suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(items, f)
            os.replace(tmp, _path(source_name))  # atomic — readers never see a partial file
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def recent(source_name: str, limit: int = 50) -> list[dict]:
    """Most-recent-first sandbox detections for a source (empty if none)."""
    try:
        with open(_path(source_name)) as f:
            data = json.load(f)
        return data[:limit] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def clear(source_name: str) -> None:
    """Drop a source's sandbox feed (called when it goes live)."""
    try:
        os.unlink(_path(source_name))
    except OSError:
        pass
