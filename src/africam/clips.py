from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from africam.audio.source import AudioChunk


def save_chunk_wav(chunk: AudioChunk, root: Path) -> Path:
    """Persist a chunk to disk as a 16-bit PCM WAV. Returns the absolute path."""
    day = chunk.started_at.strftime("%Y-%m-%d")
    out_dir = root / chunk.source_name / day
    out_dir.mkdir(parents=True, exist_ok=True)

    fname = chunk.started_at.strftime("%Y%m%dT%H%M%S_%fZ.wav")
    path = out_dir / fname

    pcm = np.clip(chunk.samples, -1.0, 1.0)
    pcm_i16 = (pcm * 32767.0).astype(np.int16)

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(chunk.sample_rate)
        w.writeframes(pcm_i16.tobytes())

    return path
