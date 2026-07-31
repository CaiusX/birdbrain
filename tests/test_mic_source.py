from __future__ import annotations

from birdbrain.audio import MicSource
from birdbrain.audio.source import AudioSource


def test_mic_source_is_an_audio_source():
    src = MicSource(name="tbb-a1b2")
    assert isinstance(src, AudioSource)
    assert src.name == "tbb-a1b2"


def test_ffmpeg_command_reads_alsa_at_the_configured_rate():
    src = MicSource(name="unit", device="plughw:2,0", sample_rate=44_100)
    cmd = src._ffmpeg_command()

    # Reads from ALSA, downmixes to mono, emits raw s16le on stdout — the exact
    # contract AudioSource.stream() depends on.
    assert cmd[0] == "ffmpeg"
    assert "-f" in cmd and "alsa" in cmd
    i = cmd.index("-i")
    assert cmd[i + 1] == "plughw:2,0"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-ar") + 1] == "44100"
    # final output spec: raw signed-16 little-endian PCM to stdout
    assert cmd[-3:] == ["-f", "s16le", "-"]


def test_default_device_and_url_label():
    src = MicSource(name="unit")
    assert src.device == "plughw:1,0"
    # current_url() must not raise (AudioSource declares a `url` attribute that
    # logging touches); MicSource gives it a descriptive value.
    assert src.current_url() == "alsa:plughw:1,0"
