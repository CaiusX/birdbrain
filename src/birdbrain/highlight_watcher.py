"""Detect when a YouTube source is showing a replayed "highlight" reel.

Some Africam-style live cams (e.g. Safarihoek) cut to pre-recorded highlight
montages when the live feed is quiet. Those montages carry their own audio
(music / narration / past wildlife), which BirdNET happily turns into bogus
detections — and because the montages vary per play, the audio-hash replay
filter can't catch them.

They DO carry a tell, though: a solid orange "HIGHLIGHT" banner in the
bottom-left corner of the video. This watcher grabs one low-resolution frame
every couple of minutes and looks for that banner by colour (no OCR / no
tesseract). The pipeline reads ``is_active()`` and skips logging detections
while a highlight is on screen, leaving the live feed at full sensitivity.

Fail-open by design: if frame grabs fail, ``is_active()`` goes stale and
returns False, so a broken watcher never silently swallows live detections.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from birdbrain.audio.youtube import _detect_js_runtime
from birdbrain.logging import get_logger

log = get_logger(__name__)

# How often to grab a frame, and how long a successful reading stays valid.
# Highlight montages run for minutes, so a 2-minute cadence catches them while
# keeping the Pi's extra video bandwidth modest. A reading older than
# STALE_AFTER_S (a few missed grabs) is treated as "unknown" → not gating.
DEFAULT_EVERY_S = 120
STALE_AFTER_S = 360


def detect_highlight_banner(png_bytes: bytes) -> bool:
    """True if the bottom-left of the frame holds the orange HIGHLIGHT banner.

    Resolution-independent: works on a relative crop, so it's robust to whatever
    size the frame was grabbed at. The banner is a saturated orange (~RGB
    221,136,61) filling a wide strip in the bottom-left; a plain colour+area
    test separates it cleanly from incidental orange (sunset sky sits at the
    top, animals are rarely this uniform)."""
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception:
        return False
    w, h = img.size
    crop = img.crop((0, int(0.74 * h), int(0.30 * w), int(0.90 * h)))
    a = np.asarray(crop).astype(int)
    if a.size == 0:
        return False
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    # Banner orange: high red, mid green, low blue, with clear R>G>B gaps.
    orange = (
        (r > 185) & (g > 95) & (g < 185) & (b < 100) & (r - g > 45) & (g - b > 30)
    )
    # The banner covers ~15% of this strip; 6% gives margin without tripping on
    # a few stray orange pixels.
    return float(orange.mean()) > 0.06


class HighlightWatcher:
    """Per-source background thread tracking whether a highlight reel is on
    screen. Single-writer / multi-reader on ``_active``/``_updated`` — the GIL
    is enough, same as SiteOcrWatcher."""

    def __init__(
        self,
        source_name: str,
        url: str,
        cookies_file: str | None = None,
        every_seconds: int = DEFAULT_EVERY_S,
        stale_after_s: int = STALE_AFTER_S,
        db=None,
    ) -> None:
        self.source_name = source_name
        self.url = url
        self.cookies_file = cookies_file
        self.every_seconds = every_seconds
        self.stale_after_s = stale_after_s
        # Optional Database — when set, each tick's state is persisted so /admin
        # can show live-vs-highlight and time-in-each.
        self.db = db
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active = False
        self._updated = 0.0  # monotonic; 0 = never read
        self._video_url: str | None = None  # cached resolved low-res URL

    def is_active(self) -> bool:
        """True only when the most recent reading saw a banner AND is fresh.
        Stale (failed grabs) or never-read → False, so we fail open."""
        if not self._active:
            return False
        return (time.monotonic() - self._updated) < self.stale_after_s

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"highlight-{self.source_name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("highlight.start", source=self.source_name, every_s=self.every_seconds)
        self._stop.wait(5.0)  # let the audio path resolve first
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("highlight.tick_failed", source=self.source_name)
            self._stop.wait(self.every_seconds)

    def _tick(self) -> None:
        png = self._grab_frame()
        if png is None:
            # Leave state as-is; staleness makes is_active() fail open.
            return
        was = self._active
        self._active = detect_highlight_banner(png)
        self._updated = time.monotonic()
        if self._active != was:
            log.info(
                "highlight.state", source=self.source_name, active=self._active
            )
        # Persist for /admin (current state + freshness + interval accounting).
        if self.db is not None:
            try:
                self.db.record_highlight_state(self.source_name, self._active)
            except Exception:
                log.exception("highlight.record_failed", source=self.source_name)

    def _grab_frame(self) -> bytes | None:
        """Grab one frame as PNG bytes from a low-res video rendition. Resolves
        (and caches) the stream URL; on ffmpeg failure, drops the cache so the
        next tick re-resolves."""
        if self._video_url is None:
            self._video_url = self._resolve_video_url()
        if self._video_url is None:
            return None
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", self._video_url,
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            self._video_url = None
            return None
        if proc.returncode != 0 or not proc.stdout:
            self._video_url = None  # URL likely expired — re-resolve next tick
            return None
        return proc.stdout

    def _resolve_video_url(self) -> str | None:
        """Resolve a low-resolution (<=360p) video URL via yt-dlp. Low res keeps
        the per-grab download small. Cookies copied to a temp file so yt-dlp's
        write-back can't clobber the canonical export (same as YouTubeSource)."""
        cmd = [
            "yt-dlp",
            "-f", "best[height<=360]/worst[height>=180]/worst",
            "-g", "--no-warnings",
        ]
        tmp_cookies: Path | None = None
        if self.cookies_file and Path(self.cookies_file).is_file():
            fd, tmp_path = tempfile.mkstemp(suffix=".cookies.txt")
            import os

            os.close(fd)
            tmp_cookies = Path(tmp_path)
            shutil.copy(self.cookies_file, tmp_cookies)
            cmd += ["--cookies", str(tmp_cookies)]
        runtime = _detect_js_runtime()
        if runtime:
            cmd += ["--js-runtimes", runtime]
        cmd += [self.url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=90)
        except subprocess.TimeoutExpired:
            return None
        finally:
            if tmp_cookies is not None:
                try:
                    tmp_cookies.unlink()
                except OSError:
                    pass
        if result.returncode != 0:
            log.debug(
                "highlight.resolve_failed",
                source=self.source_name,
                err=(result.stderr or "")[:200],
            )
            return None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                return line
        return None


class HighlightGate:
    """Lifecycle manager that switches a source's HighlightWatcher on and off
    to follow the ``gate_highlights:<name>`` setting, polled by the worker. A
    fresh watcher is created on enable (threads are one-shot, so we can't
    restart a stopped one) and torn down on disable, clearing the playback
    state so /admin's badge disappears promptly."""

    def __init__(self, source_name: str, url: str, cookies_file: str | None, db) -> None:
        self.source_name = source_name
        self.url = url
        self.cookies_file = cookies_file
        self.db = db
        self._watcher: HighlightWatcher | None = None

    def update(self, enabled: bool) -> None:
        if enabled and self._watcher is None:
            self._watcher = HighlightWatcher(
                source_name=self.source_name,
                url=self.url,
                cookies_file=self.cookies_file,
                db=self.db,
            )
            self._watcher.start()
            log.info("highlight.gate_on", source=self.source_name)
        elif not enabled and self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
            try:
                self.db.clear_playback_state(self.source_name)
            except Exception:
                log.exception("highlight.clear_failed", source=self.source_name)
            log.info("highlight.gate_off", source=self.source_name)

    def is_active(self) -> bool:
        return self._watcher is not None and self._watcher.is_active()

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
