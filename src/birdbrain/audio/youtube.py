from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from birdbrain.audio.source import AudioSource
from birdbrain.logging import get_logger

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
        # yt-dlp writes back to the cookies file at the end of each session,
        # which gradually clobbers the canonical export when YouTube rejects
        # auth. Work around that by copying the file to a per-invocation temp
        # path: yt-dlp can write to its heart's content, the canonical export
        # at self.cookies_file stays pristine for the next call.
        tmp_cookies: Path | None = None
        if self.cookies_file:
            src = Path(self.cookies_file)
            if src.is_file():
                fd, tmp_path = tempfile.mkstemp(suffix=".cookies.txt")
                tmp_cookies = Path(tmp_path)
                import os
                os.close(fd)
                shutil.copy(src, tmp_cookies)
                cmd += ["--cookies", str(tmp_cookies)]
            else:
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
        # Force a single player client for live-stream format resolution. As of
        # 2026-08 (yt-dlp 2026.07.04) the mweb/tv/web/web_safari clients all
        # return "No video formats found!" for these YouTube live streams, while
        # android_vr still publishes a working HLS manifest (cookie-free). We pin
        # one client (rather than the multi-client default) to keep each resolve
        # to a single query: on a pipeline restart every source re-resolves at
        # once, and multiplying the YouTube request burst raises the odds of
        # tripping the IP bot-block. YouTube rotates which clients work — if this
        # starts failing "No video formats found!" again, first `uv run yt-dlp -U`,
        # then re-test clients (`--extractor-args youtube:player_client=<name>`)
        # and update the one below.
        cmd += ["--extractor-args", "youtube:player_client=android_vr"]
        cmd += [self.url]

        try:
            # Bound the resolve. Without a timeout a hung yt-dlp (seen when
            # YouTube rate-limits the IP) blocks the worker thread forever; the
            # supervisor's is_alive() check then can't tell it apart from a
            # healthy worker, so the source stays dark indefinitely. A timeout
            # turns that into a normal failure → backoff → retry → recovery.
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=90
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"yt-dlp timed out resolving {self.url}") from e
        finally:
            if tmp_cookies is not None:
                try:
                    tmp_cookies.unlink()
                except OSError:
                    pass
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
            # Bound the reconnect so ffmpeg can't loop forever on a dead/expired
            # manifest (YouTube live URLs expire after a few hours). After a few
            # failed retries, or rw_timeout with no data, ffmpeg exits cleanly;
            # the worker's retry loop then re-resolves a FRESH URL via yt-dlp.
            # Without these a stalled stream wedges ffmpeg indefinitely, leaking
            # processes and memory that never EOF.
            "-reconnect_max_retries", "5",
            "-rw_timeout", "30000000",  # 30s (microseconds) with no data → fail
            "-i", stream_url,
            "-vn",
            "-ac", "1",
            "-ar", str(self.sample_rate),
            "-f", "s16le",
            "-",
        ]
