"""Guards on the pinned YouTube player client.

YouTube rotates which client publishes a working HLS manifest, and whether that
client will accept the cookies file. Getting the cookie half wrong looks exactly
like getting the client half wrong -- "No video formats found!" on every source
-- so the two are pinned together and asserted together.
"""

from __future__ import annotations

from birdbrain.audio import youtube as yt


class TestResolveArgs:
    def test_pins_exactly_one_player_client(self):
        args = yt.resolve_args()
        assert "--extractor-args" in args
        pinned = args[args.index("--extractor-args") + 1]
        assert pinned == f"youtube:player_client={yt.PLAYER_CLIENT}"
        assert "," not in pinned, "one client per resolve keeps the request burst small"


class TestCookieSuppression:
    def _cmd(self, monkeypatch, *, accepts: bool, cookies_file: str) -> list[str]:
        """The argv _resolve_stream_url would run, without running it."""
        monkeypatch.setattr(yt, "PLAYER_CLIENT_ACCEPTS_COOKIES", accepts)
        seen: dict[str, list[str]] = {}

        class _Res:
            returncode = 0
            stdout = "https://example.invalid/manifest.m3u8\n"
            stderr = ""

        def _run(cmd, **kw):
            seen["cmd"] = list(cmd)
            return _Res()

        monkeypatch.setattr(yt.subprocess, "run", _run)
        src = yt.YouTubeSource(
            name="cam", url="https://www.youtube.com/watch?v=aaaaaaaaaaa",
            cookies_file=cookies_file,
        )
        src._resolve_stream_url()
        return seen["cmd"]

    def test_anonymous_client_is_given_no_cookies(self, tmp_path, monkeypatch):
        """THE regression. android resolves every cam anonymously but is refused
        when handed the cookies file, so a configured cookies_file must not
        reach it."""
        jar = tmp_path / "cookies.txt"
        jar.write_text("# Netscape HTTP Cookie File\n")
        cmd = self._cmd(monkeypatch, accepts=False, cookies_file=str(jar))
        assert "--cookies" not in cmd
        assert "--cookies-from-browser" not in cmd

    def test_cookie_taking_client_still_gets_them(self, tmp_path, monkeypatch):
        jar = tmp_path / "cookies.txt"
        jar.write_text("# Netscape HTTP Cookie File\n")
        cmd = self._cmd(monkeypatch, accepts=True, cookies_file=str(jar))
        assert "--cookies" in cmd
