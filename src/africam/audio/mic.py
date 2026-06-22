from __future__ import annotations

from africam.audio.source import AudioSource


class MicSource(AudioSource):
    """Capture audio from a local USB microphone via ffmpeg's ALSA input.

    This is the one new audio source a TinyBirdBrain (TBB) unit needs. Everything
    downstream — the chunker, :class:`~africam.detector.BirdNetDetector`, and the
    clip writer — is unchanged; only the ffmpeg command differs, reading from an
    ALSA device instead of a network URL.
    """

    def __init__(
        self,
        name: str,
        device: str = "plughw:1,0",
        sample_rate: int = 48_000,
        chunk_seconds: float = 3.0,
    ) -> None:
        super().__init__(name=name, sample_rate=sample_rate, chunk_seconds=chunk_seconds)
        # ALSA device of the USB mic, e.g. "plughw:1,0" (card 1, device 0). The
        # "plughw" plugin lets ALSA resample/convert to the rate ffmpeg requests.
        self.device = device
        # No network URL for a mic; expose the device so logging/current_url have
        # something meaningful rather than an unset attribute.
        self.url = f"alsa:{device}"

    def _ffmpeg_command(self) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "alsa",
            "-i", self.device,
            "-ac", "1",
            "-ar", str(self.sample_rate),
            "-f", "s16le",
            "-",
        ]
