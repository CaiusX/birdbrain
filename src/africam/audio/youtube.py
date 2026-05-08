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

    def _resolve_stream_url(self) -> str:
        result = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "-g", "--no-warnings", self.url],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"yt-dlp failed for {self.url}: {result.stderr.strip() or result.stdout.strip()}"
            )
        # bestaudio is one URL; for some live streams yt-dlp emits two (audio+video). Take the first.
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                return line
        raise RuntimeError(f"yt-dlp returned no stream URL for {self.url}")

    def _ffmpeg_command(self) -> list[str]:
        stream_url = self._resolve_stream_url()
        log.info("youtube.resolved", source=self.name, url=stream_url[:80] + "...")
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
