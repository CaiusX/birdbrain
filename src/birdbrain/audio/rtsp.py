from __future__ import annotations

from birdbrain.audio.source import AudioSource


class RtspSource(AudioSource):
    """Stream audio directly from an RTSP IP camera via ffmpeg."""

    def __init__(
        self,
        name: str,
        url: str,
        sample_rate: int = 48_000,
        chunk_seconds: float = 3.0,
        transport: str = "tcp",
    ) -> None:
        super().__init__(name=name, sample_rate=sample_rate, chunk_seconds=chunk_seconds)
        self.url = url
        self.transport = transport

    def _ffmpeg_command(self) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", self.transport,
            "-i", self.url,
            "-vn",
            "-ac", "1",
            "-ar", str(self.sample_rate),
            "-f", "s16le",
            "-",
        ]
