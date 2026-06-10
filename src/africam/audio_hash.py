"""Perceptual fingerprint for detection clips, used to catch ad / highlight
replays on YouTube streams.

The idea: when a stream airs the same ad or replays a highlight reel, our
3 s chunker turns the (essentially identical) audio into multiple "detections"
at different timestamps. Computing a content-derived hash on each saved clip
lets us tell those apart from genuinely repeated calls — the same audio gives
the same hash regardless of when it played.

Design choices:

  * **Mel-spectrogram + median threshold** rather than chromaprint. Librosa
    and numpy are already in the dependency tree (no system libchromaprint to
    install on the Pi) and an exact ad-replay hits identical PCM in the mel
    grid every time.
  * **Fixed output shape (16 × 16 = 256 bits)**: pad/truncate the input to
    exactly 6 seconds at 16 kHz before the spectrogram, so a clip with a
    chopped tail still hashes to the same fingerprint as a clean one.
  * **Per-frame median threshold** ("perceptual hash" trick): robust to
    overall gain drift / re-encoding but bit-perfect for true byte-identical
    replays. Two replays of a single ad agree to 0 bits of Hamming distance
    in practice; we treat hash equality as exact-match in SQL.

Output: 64-char lowercase hex string (256 bits / 32 bytes). ``None`` if the
clip can't be read or is degenerate (e.g. all-silence file produces a
useless hash — explicitly returned as None so it doesn't false-collide).
"""
from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

# Resampling and framing constants. Held global so the hash is deterministic
# across releases — if you bump any of these, ALL existing hashes become
# meaningless and the backfill must be re-run.
_TARGET_SR = 16_000
_TARGET_SECS = 6.0
_TARGET_LEN = int(_TARGET_SR * _TARGET_SECS)  # 96 000 samples
_N_MELS = 16
_N_FRAMES = 16
_WIN_LENGTH = 2_048


def clip_hash(path: str | Path) -> str | None:
    """Return a 64-char hex perceptual fingerprint for ``path``, or None.

    Returns None on:
      * I/O / decode error (missing file, corrupt OGG, etc.)
      * essentially silent input (would produce a hash that collides with
        every other silent clip — useless for dedup)
    """
    try:
        y, sr = sf.read(str(path), dtype="float32", always_2d=False)
    except Exception:
        return None
    if y.size == 0:
        return None

    # Downmix to mono if stereo / multichannel.
    if y.ndim > 1:
        y = y.mean(axis=1)

    # Resample to a fixed SR so the hash is decoupled from the source SR.
    if sr != _TARGET_SR:
        try:
            y = librosa.resample(y, orig_sr=sr, target_sr=_TARGET_SR)
        except Exception:
            return None

    # Pad shorter clips with silence; truncate longer ones. Replays produce
    # identical leading 6 s, so right-truncation is fine.
    if len(y) < _TARGET_LEN:
        y = np.pad(y, (0, _TARGET_LEN - len(y)))
    else:
        y = y[:_TARGET_LEN]

    # Reject degenerate near-zero input. We deliberately set the threshold
    # very low (-80 dBFS RMS) — outdoor wildlife clips routinely sit at
    # -50 to -60 dB and are still useful for fingerprinting. Anything
    # below -80 dB is effectively a digital silence file where the median-
    # threshold step below becomes numerically unstable.
    rms = float(np.sqrt(np.mean(y * y) + 1e-12))
    if rms < 10 ** (-80 / 20):
        return None

    hop_length = _TARGET_LEN // _N_FRAMES  # 6000 samples per frame
    try:
        S = librosa.feature.melspectrogram(
            y=y,
            sr=_TARGET_SR,
            n_mels=_N_MELS,
            hop_length=hop_length,
            win_length=_WIN_LENGTH,
            n_fft=_WIN_LENGTH,
        )
    except Exception:
        return None

    # librosa appends a partial trailing frame; trim to a deterministic shape.
    if S.shape[1] < _N_FRAMES:
        return None
    S = S[:, :_N_FRAMES]

    # dB scale, then per-row (per-mel-band) median threshold. The median is
    # within-frequency so a bandwise gain drift doesn't flip every bit.
    S_db = librosa.power_to_db(S)
    medians = np.median(S_db, axis=1, keepdims=True)
    bits = (S_db > medians).astype(np.uint8).flatten()
    packed = np.packbits(bits)  # 256 bits -> 32 bytes
    return packed.tobytes().hex()
