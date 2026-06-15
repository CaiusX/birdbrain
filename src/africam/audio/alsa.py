from __future__ import annotations

from africam.audio.source import AudioSource


class AlsaSource(AudioSource):
    """Capture audio from a locally-connected ALSA device (e.g. a USB mic).

    ``url`` is an ALSA device string rather than a network URL — prefer the
    by-name form ``plughw:CARD=<name>,DEV=0`` so it survives the card index
    shuffling around on reboot/replug; ``plughw`` (vs ``hw``) lets ALSA convert
    rate/format/channels if the device doesn't natively match what we request.

    Much lighter than the streaming sources: no URL resolution, no network, no
    reconnect logic — ffmpeg reads the device and emits PCM until it's stopped.
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

    def _ffmpeg_command(self) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "alsa",
            # Decouple the ALSA capture thread from downstream so a momentary
            # stall doesn't drop samples ("ALSA buffer xrun").
            "-thread_queue_size", "1024",
            "-i", self.url,
            "-ac", "1",
            "-ar", str(self.sample_rate),
            "-f", "s16le",
            "-",
        ]
