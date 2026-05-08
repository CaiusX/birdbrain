from __future__ import annotations

import subprocess

from africam.audio.source import AudioSource
from africam.logging import get_logger

log = get_logger(__name__)


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
    ) -> None:
        super().__init__(name=name, sample_rate=sample_rate, chunk_seconds=chunk_seconds)
        self.url = url

    def current_url(self) -> str:
        return self._resolve_stream_url()

    def _resolve_stream_url(self) -> str:
        # bestaudio/best: prefer audio-only when present, otherwise an A+V manifest
        # which ffmpeg will demux (we drop video with -vn). Required because many
        # YouTube live streams don't publish a standalone audio format.
        result = subprocess.run(
            ["yt-dlp", "-f", "bestaudio/best", "-g", "--no-warnings", self.url],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"yt-dlp failed for {self.url}: {result.stderr.strip() or result.stdout.strip()}"
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
