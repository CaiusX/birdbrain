from __future__ import annotations

import contextlib
import io
import threading
from dataclasses import dataclass
from datetime import datetime

from birdnetlib import RecordingBuffer
from birdnetlib.analyzer import Analyzer

from africam.audio.source import AudioChunk
from africam.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Detection:
    source_name: str
    started_at: datetime  # UTC
    duration_s: float
    scientific_name: str
    common_name: str
    confidence: float


@contextlib.contextmanager
def _silence_stdout():
    """Swallow birdnetlib's bare-`print()` trace lines without affecting our logs."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


class BirdNetDetector:
    """Wraps :mod:`birdnetlib` to run BirdNET on streaming numpy buffers.

    A single :class:`Analyzer` is shared across calls and inference is serialised
    by ``self._lock`` — the underlying TFLite interpreter is not safe for
    concurrent ``invoke`` calls, but is cheap to share between threads otherwise.
    """

    def __init__(self) -> None:
        log.info("birdnet.load_start")
        with _silence_stdout():
            self._analyzer = Analyzer()
        self._lock = threading.Lock()
        log.info("birdnet.load_done")

    def analyze(
        self,
        chunk: AudioChunk,
        *,
        lat: float | None = None,
        lon: float | None = None,
        week: int | None = None,
        min_confidence: float = 0.5,
    ) -> list[Detection]:
        with self._lock, _silence_stdout():
            recording = RecordingBuffer(
                self._analyzer,
                chunk.samples,
                chunk.sample_rate,
                lat=lat,
                lon=lon,
                week_48=week if week is not None else -1,
                min_conf=min_confidence,
                date=chunk.started_at,
            )
            recording.analyze()
            raw = list(recording.detections)

        out: list[Detection] = []
        for d in raw:
            sci = d.get("scientific_name")
            common = d.get("common_name")
            if sci is None or common is None:
                # Fallback to the "Scientific_Common" label if explicit fields are missing.
                label = d.get("label", "")
                sci = sci or (label.split("_", 1)[0] if "_" in label else label)
                common = common or (label.split("_", 1)[1] if "_" in label else label)
            out.append(
                Detection(
                    source_name=chunk.source_name,
                    started_at=chunk.started_at,
                    duration_s=chunk.duration_s,
                    scientific_name=sci,
                    common_name=common,
                    confidence=float(d["confidence"]),
                )
            )
        return out
