"""Frame-grabbing OCR for detecting which site a multi-site stream is showing.

Pulls a single video frame from the source via ffmpeg, runs tesseract through
pytesseract, and matches the recognised text against the configured site
aliases. Designed to be run in its own thread per source.

If the system tesseract binary isn't installed (or pytesseract can't find it)
the watcher logs a warning once and stops — the resolver will fall back to
manual mode automatically.
"""
from __future__ import annotations

import io
import subprocess
import threading
import time
from collections import Counter
from datetime import UTC, datetime

from PIL import Image

from africam.config import OcrConfig, SourceConfig
from africam.logging import get_logger
from africam.sites import Site

log = get_logger(__name__)


class SiteOcrWatcher:
    """Background loop that periodically updates ``self.latest`` with the most
    recently OCR-detected site. Resolver code reads ``latest`` without locking
    semantics beyond the GIL — single-writer, multi-reader is fine here."""

    def __init__(
        self,
        source: SourceConfig,
        sites: dict[str, Site],
        ocr_cfg: OcrConfig,
        resolve_stream_url,  # callable: () -> str
    ) -> None:
        self.source = source
        self.sites = sites
        self.cfg = ocr_cfg
        self._resolve = resolve_stream_url
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._latest: tuple[Site, datetime] | None = None
        self._streak: Counter[str] = Counter()

    @property
    def latest(self) -> tuple[Site, datetime] | None:
        return self._latest

    def start(self) -> None:
        if self._thread is not None:
            return
        # Sanity-check: is tesseract reachable? If not, don't even start.
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
        except Exception as e:
            log.warning(
                "ocr.disabled",
                source=self.source.name,
                reason=f"tesseract unavailable: {e}",
            )
            return

        self._thread = threading.Thread(
            target=self._loop,
            name=f"ocr-{self.source.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("ocr.start", source=self.source.name, every_s=self.cfg.every_seconds)
        # Stagger first OCR so multiple watchers don't all hit ffmpeg at once on startup.
        self._stop.wait(2.0)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("ocr.tick_failed", source=self.source.name)
            self._stop.wait(self.cfg.every_seconds)

    def _tick(self) -> None:
        text = self._read_caption_text()
        if not text:
            return
        site = self._match_site(text)
        slog = log.bind(source=self.source.name)
        if site is None:
            slog.debug("ocr.no_match", text=text[:80])
            self._streak.clear()
            return
        # Streak gate: require N consecutive matches before promoting.
        self._streak[site.name] += 1
        if self._streak[site.name] >= self.cfg.confirm_count:
            self._latest = (site, datetime.now(UTC))
            slog.info("ocr.matched", site=site.name, streak=self._streak[site.name])
            self._streak.clear()

    def _read_caption_text(self) -> str | None:
        """Grab a single frame and OCR the optional crop region."""
        import pytesseract

        stream_url = self._resolve()
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", stream_url,
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=20)
        if proc.returncode != 0 or not proc.stdout:
            log.debug("ocr.frame_failed", source=self.source.name, rc=proc.returncode)
            return None
        try:
            img = Image.open(io.BytesIO(proc.stdout))
            img.load()
        except Exception:
            return None
        if self.cfg.crop:
            x, y, w, h = self.cfg.crop
            img = img.crop((x, y, x + w, y + h))
        # Greyscale + light upscale tends to help tesseract on small captions.
        img = img.convert("L").resize((img.width * 2, img.height * 2))
        return pytesseract.image_to_string(img).strip() or None

    def _match_site(self, text: str) -> Site | None:
        haystack = text.lower()
        # Longest-alias-first wins to avoid e.g. "Olifants" matching "Olifants West"
        # only as the parent reserve.
        candidates = [
            (alias, site)
            for site in self.sites.values()
            for alias in site.all_terms
        ]
        candidates.sort(key=lambda pair: -len(pair[0]))
        for alias, site in candidates:
            if alias.lower() in haystack:
                return site
        return None
