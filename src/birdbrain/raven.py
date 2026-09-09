"""Raven Pro selection tables, for reviewing our detections in real bioacoustics
software.

Raven Pro is a desktop application with no API, so the only way in is a file it
already reads: a *sound selection table*. One table can span many audio files —
each row carries the file it belongs to and the offset within it — which is
exactly the shape of "every clip of one species at one site".

Two facts about our clips decide the geometry, and both are easy to get wrong:

* **A detection occupies the LAST ``duration_s`` seconds of its clip.** The
  pipeline prepends the previous chunk as pre-roll, so a 6 s clip is 3 s of
  context followed by the 3 s BirdNET actually fired on.
* **The filename is not a reliable clock.** For locally captured clips the
  pipeline shifts the name back by the pre-roll, so name + 3 s = detection.
  For clips pushed from an ingest node, central names the file after the
  detection's own timestamp, so name = detection. Deriving the window from the
  filename therefore lands 3 s out on 80% of the corpus. Measuring from the end
  of the file is correct for both, and was verified by re-running BirdNET over
  each half of real clips of both kinds: the species fires in the second half
  every time, recovering the stored confidence.

Times are seconds, frequencies Hz, and the file is tab-delimited — Raven
rejects a header without real delimiters between the column names, which is the
bug that makes BirdNET-Analyzer's own Raven export unreadable in Raven 1.6.

Audio is transcoded on the way out. Raven reads WAVE, AIFF, FLAC and MP3; it
does not read the OGG Vorbis we store, so an export of the raw clips opens as
a folder of errors. FLAC is the target: lossless, about half the size of WAV,
and the one supported format Cornell lists no caveats against (they warn off
"extensible" WAVE, and MP3 would pile a second lossy pass on audio that is
already Vorbis). Transcoding restores nothing — the Vorbis artefacts are in
the stored clip and will be visible in Raven's spectrogram — but it adds none.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from birdbrain.audio.locator import DEFAULT_BAND, SPECIES_FREQ_BANDS

#: What Raven can open, and what we hand it. FLAC by default; WAV as an escape
#: hatch if a particular Raven build is unhappy with our FLAC.
EXPORT_FORMATS = {"flac": ("FLAC", ".flac"), "wav": ("WAV", ".wav")}
DEFAULT_EXPORT_FORMAT = "flac"

#: Raven's required columns, then the file-linking ones, then ours. Raven shows
#: unknown columns verbatim and keeps them on save, which is how the operator's
#: verdict gets back to us.
COLUMNS = [
    "Selection",
    "View",
    "Channel",
    "Begin Time (s)",
    "End Time (s)",
    "Low Freq (Hz)",
    "High Freq (Hz)",
    "Begin File",
    "File Offset (s)",
    "Species",
    "Common Name",
    "Confidence",
    "Detection ID",
    "Site",
    "Start (UTC)",
    "Label",
]

VIEW = "Spectrogram 1"
CHANNEL = 1


@dataclass(frozen=True)
class Clip:
    """One detection and the audio file holding it."""

    detection_id: int
    path: Path
    duration_s: float          # of the FILE, not the detection window
    window_s: float            # the BirdNET window, normally 3.0
    scientific_name: str
    common_name: str
    confidence: float
    source_name: str
    started_at: str
    label: str | None = None
    #: Name the audio takes inside the export. Differs from ``path.name``
    #: because the clip is transcoded out of OGG, which Raven cannot read, and
    #: the selection table has to name the file the reviewer actually opens.
    export_name: str | None = None

    @property
    def audio_name(self) -> str:
        return self.export_name or self.path.name


def band_for(scientific_name: str) -> tuple[float, float]:
    """The frequency band Raven should box for this species — the same table the
    review page's locator overlay uses, so the two agree."""
    return SPECIES_FREQ_BANDS.get(scientific_name, DEFAULT_BAND)


def selection_table(clips: list[Clip]) -> str:
    """A Raven sound selection table covering every clip, in order.

    ``Begin Time`` is cumulative across the file sequence (what Raven shows when
    the files are opened together); ``File Offset`` is the position within the
    individual file. Both are needed: Raven uses the first to place the box and
    the second to know where in the file it came from.
    """
    out = io.StringIO()
    w = csv.writer(out, delimiter="\t", lineterminator="\n")
    w.writerow(COLUMNS)
    cursor = 0.0
    for i, c in enumerate(clips, start=1):
        # The window sits at the END of the file — see the module docstring.
        offset = max(0.0, c.duration_s - c.window_s)
        lo, hi = band_for(c.scientific_name)
        w.writerow([
            i,
            VIEW,
            CHANNEL,
            f"{cursor + offset:.4f}",
            f"{cursor + offset + c.window_s:.4f}",
            f"{lo:.1f}",
            f"{hi:.1f}",
            c.audio_name,
            f"{offset:.4f}",
            c.scientific_name,
            c.common_name,
            f"{c.confidence:.4f}",
            c.detection_id,
            c.source_name,
            c.started_at,
            c.label or "",
        ])
        cursor += c.duration_s
    return out.getvalue()


def transcode(path: Path, fmt: str = DEFAULT_EXPORT_FORMAT) -> tuple[str, bytes]:
    """(filename, bytes) of one clip in a format Raven can open.

    Decoded and re-encoded in memory rather than through a temp file: the
    clips are seconds long, and an export is hundreds of them.
    """
    import soundfile as sf  # noqa: PLC0415 - keeps libsndfile off the import path

    subtype, suffix = EXPORT_FORMATS.get(fmt, EXPORT_FORMATS[DEFAULT_EXPORT_FORMAT])
    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    buf = io.BytesIO()
    # PCM_16 for both: FLAC has no float subtype in libsndfile, and 16-bit is
    # what the source Vorbis decodes to any useful precision anyway.
    sf.write(buf, data, sr, format=subtype, subtype="PCM_16")
    return path.with_suffix(suffix).name, buf.getvalue()
