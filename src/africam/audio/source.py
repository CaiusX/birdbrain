from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from africam.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class AudioChunk:
    """A fixed-duration mono PCM chunk in float32 [-1, 1]."""

    samples: np.ndarray  # shape: (n_samples,), dtype=float32
    sample_rate: int
    started_at: datetime  # UTC, wall clock at start of the chunk
    source_name: str

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate


class AudioSource(ABC):
    """Base class for streaming audio sources.

    Concrete sources implement :meth:`_ffmpeg_command` to return an ffmpeg invocation
    that emits mono signed-16-bit little-endian PCM at ``sample_rate`` Hz on stdout.
    """

    def __init__(self, name: str, sample_rate: int = 48_000, chunk_seconds: float = 3.0) -> None:
        self.name = name
        self.sample_rate = sample_rate
        self.chunk_seconds = chunk_seconds
        self._chunk_samples = int(round(sample_rate * chunk_seconds))
        self._chunk_bytes = self._chunk_samples * 2  # int16 mono

    @abstractmethod
    def _ffmpeg_command(self) -> list[str]: ...

    def stream(self) -> Iterator[AudioChunk]:
        """Yield :class:`AudioChunk` instances until the underlying ffmpeg process exits."""
        cmd = self._ffmpeg_command()
        log.info("ffmpeg.start", source=self.name, cmd=cmd)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        assert proc.stdout is not None
        try:
            while True:
                buf = self._read_exact(proc.stdout, self._chunk_bytes)
                if buf is None:
                    log.warning("ffmpeg.eof", source=self.name)
                    return
                samples = (
                    np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                )
                yield AudioChunk(
                    samples=samples,
                    sample_rate=self.sample_rate,
                    started_at=datetime.now(UTC),
                    source_name=self.name,
                )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    @staticmethod
    def _read_exact(stream, n: int) -> bytes | None:
        """Read exactly ``n`` bytes from ``stream``. Return ``None`` on EOF."""
        out = bytearray()
        while len(out) < n:
            chunk = stream.read(n - len(out))
            if not chunk:
                return None
            out.extend(chunk)
        return bytes(out)
