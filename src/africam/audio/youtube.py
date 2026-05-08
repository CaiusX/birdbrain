from __future__ import annotations

import shutil
import subprocess

from africam.audio.source import AudioSource
from africam.logging import get_logger

log = get_logger(__name__)


def _detect_js_runtime() -> str | None:
    """Return a yt-dlp ``--js-runtimes`` argument pointing at any JS runtime
    found on PATH. yt-dlp auto-uses Deno but needs an explicit hint for Node;
    YouTube's recent ``n``-challenge means we now need one or the other."""
    for name in ("deno", "node"):
        path = shutil.which(name)
        if path:
            return f"{name}:{path}"
    return None


class YouTubeSource(AudioSource):
    """Stream audio from a YouTube URL (live or VOD).

    Uses yt-dlp to resolve the bestaudio HLS/DASH manifest URL, then pipes the
    selected stream through ffmpeg to produce 16-bit PCM at the target rate.
    """

    def __init__(
        self,
        name: str,
        url: str,
        sample_rate: int = 48_000,
        chunk_seconds: float = 3.0,
        cookies_from_browser: str | None = None,
        cookies_file: str | None = None,
    ) -> None:
        super().__init__(name=name, sample_rate=sample_rate, chunk_seconds=chunk_seconds)
        self.url = url
        self.cookies_from_browser = cookies_from_browser
        self.cookies_file = cookies_file

    def current_url(self) -> str:
        return self._resolve_stream_url()

    def _resolve_stream_url(self) -> str:
        # bestaudio/best: prefer audio-only when present, otherwise an A+V manifest
        # which ffmpeg will demux (we drop video with -vn). Required because many
        # YouTube live streams don't publish a standalone audio format.
        cmd = ["yt-dlp", "-f", "bestaudio/best", "-g", "--no-warnings"]
        # cookies_file wins if both are set — it's the more reliable path on Windows.
        if self.cookies_file:
            cmd += ["--cookies", str(self.cookies_file)]
        elif self.cookies_from_browser:
            cmd += ["--cookies-from-browser", self.cookies_from_browser]
        # Tell yt-dlp where to find a JS runtime so it can solve YouTube's
        # n-sig challenge. Deno is auto-detected; for Node we have to be
        # explicit. The EJS solver script itself is cached on disk after a
        # one-time download via --remote-components.
        runtime = _detect_js_runtime()
        if runtime:
            cmd += ["--js-runtimes", runtime]
        cmd += [self.url]

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"yt-dlp failed for {self.url}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        # When yt-dlp emits multiple URLs (e.g. video + audio manifests for DASH),
        # we still take the first. For HLS combined streams there's only one.
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                return line
        raise RuntimeError(f"yt-dlp returned no stream URL for {self.url}")

    def _ffmpeg_command(self) -> list[str]:
        stream_url = self._resolve_stream_url()
        log.info("youtube.resolved", source=self.name, url_head=stream_url[:80])
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-i", stream_url,
            "-vn",
            "-ac", "1",
            "-ar", str(self.sample_rate),
            "-f", "s16le",
            "-",
        ]
