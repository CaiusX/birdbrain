from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import soundfile as sf

from africam.audio.source import AudioChunk

ClipFormat = Literal["ogg", "wav", "flac"]

# soundfile takes (format, subtype) tuples per container. Quality at default
# settings: OGG ≈ ~30 KB / 3 s mono @ 48 kHz, FLAC ≈ ~150 KB, WAV ≈ ~280 KB.
_FORMATS: dict[str, tuple[str, str]] = {
    "ogg":  ("OGG", "VORBIS"),
    "wav":  ("WAV", "PCM_16"),
    "flac": ("FLAC", "PCM_16"),
}


def save_chunk(chunk: AudioChunk, root: Path, fmt: ClipFormat = "ogg") -> Path:
    """Persist a chunk to disk in ``fmt`` (default: OGG Vorbis). Returns the
    absolute path. Existing clips in other formats keep working — the rest of
    the system reads whatever extension is recorded in ``DetectionRow.clip_path``.
    """
    if fmt not in _FORMATS:
        raise ValueError(f"unsupported clip format: {fmt!r}")
    container, subtype = _FORMATS[fmt]

    day = chunk.started_at.strftime("%Y-%m-%d")
    out_dir = root / chunk.source_name / day
    out_dir.mkdir(parents=True, exist_ok=True)

    fname = chunk.started_at.strftime(f"%Y%m%dT%H%M%S_%fZ.{fmt}")
    path = out_dir / fname

    pcm = np.clip(chunk.samples, -1.0, 1.0).astype(np.float32)
    sf.write(str(path), pcm, chunk.sample_rate, format=container, subtype=subtype)
    return path
