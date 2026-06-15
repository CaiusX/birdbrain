from __future__ import annotations

import subprocess
import threading
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

    url: str

    def __init__(self, name: str, sample_rate: int = 48_000, chunk_seconds: float = 3.0) -> None:
        self.name = name
        self.sample_rate = sample_rate
        self.chunk_seconds = chunk_seconds
        self._chunk_samples = int(round(sample_rate * chunk_seconds))
        self._chunk_bytes = self._chunk_samples * 2  # int16 mono

    @abstractmethod
    def _ffmpeg_command(self) -> list[str]: ...

    def current_url(self) -> str:
        """Return a URL ffmpeg can read from. Subclasses with expiring URLs
        (e.g. YouTube live manifests) should override this to re-resolve."""
        return self.url

    def stream(self, stop_event=None) -> Iterator[AudioChunk]:
        """Yield :class:`AudioChunk` instances until the underlying ffmpeg
        process exits or ``stop_event`` is set. Setting the event terminates
        the ffmpeg subprocess so the read returns immediately."""
        cmd = self._ffmpeg_command()
        log.info(
            "ffmpeg.start",
            source=self.name,
            sample_rate=self.sample_rate,
            chunk_seconds=self.chunk_seconds,
        )
        log.debug("ffmpeg.cmd", source=self.name, cmd=cmd)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        assert proc.stdout is not None

        # Watcher thread: when stop_event fires, terminate ffmpeg so the read
        # in _read_exact returns EOF. Without this, a worker blocked inside
        # _read_exact on a stalled HLS stream never notices stop_event (the
        # is_set() check at the top of the loop is unreachable while blocked),
        # and the thread + subprocess leak until process exit.
        done = threading.Event()

        def _stop_watcher() -> None:
            while not done.wait(1.0):
                if stop_event is not None and stop_event.is_set():
                    if proc.poll() is None:
                        proc.terminate()
                        # A wedged ffmpeg (stuck retrying a dead/expired URL)
                        # can ignore SIGTERM; escalate to SIGKILL so it can't
                        # linger and leak. Without this the worker thread stays
                        # blocked in _read_exact, so the finally-block's kill
                        # never runs and the process survives for hours.
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    return

        if stop_event is not None:
            threading.Thread(
                target=_stop_watcher, name=f"ffstop-{self.name}", daemon=True
            ).start()

        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
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
            done.set()
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
