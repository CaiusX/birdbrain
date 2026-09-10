"""Regression tests for the two loops that kept a YouTube IP block alive.

Both bugs had the same shape: a failure path that quietly reset itself, so the
retries meant to recover from a block were the thing sustaining it. Neither was
covered by a test, and the fleet ran ~320 blocked requests/hour for 20 hours.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from birdbrain.pipeline import (
    AUTH_BACKOFF_INITIAL,
    AUTH_BACKOFF_MAX,
    NORMAL_BACKOFF_INITIAL,
    NORMAL_BACKOFF_MAX,
    _next_backoff,
)

BOT_GATE = (
    "yt-dlp failed for https://www.youtube.com/watch?v=NapztoCaKFY: ERROR: "
    "[youtube] NapztoCaKFY: Sign in to confirm you're not a bot. Use "
    "--cookies-from-browser or --cookies for the authentication."
)


class TestAuthBackoff:
    def test_bot_gate_escalates_even_after_a_long_attempt(self):
        """THE regression. A blocked resolve takes ~3 min to fail, so ran_for
        clears the 60 s healthy-stretch bar. The auth schedule must still win —
        when the reset was tested first, this returned 5.0 forever."""
        assert _next_backoff(NORMAL_BACKOFF_INITIAL, BOT_GATE, ran_for=190.0) == (
            AUTH_BACKOFF_INITIAL
        )

    def test_bot_gate_escalates_after_a_short_attempt_too(self):
        assert _next_backoff(NORMAL_BACKOFF_INITIAL, BOT_GATE, ran_for=2.0) == (
            AUTH_BACKOFF_INITIAL
        )

    def test_repeated_bot_gates_double_up_to_the_cap(self):
        b = _next_backoff(NORMAL_BACKOFF_INITIAL, BOT_GATE, ran_for=190.0)
        seen = [b]
        for _ in range(8):
            b = _next_backoff(b, BOT_GATE, ran_for=190.0)
            seen.append(b)
        assert seen[1] == AUTH_BACKOFF_INITIAL * 2
        assert b == AUTH_BACKOFF_MAX
        assert all(x <= AUTH_BACKOFF_MAX for x in seen)

    def test_the_schedule_never_collapses_back_to_seconds(self):
        """Whatever the run length, a bot-gate never yields a sub-minute wait —
        the property that actually protects the IP."""
        b = NORMAL_BACKOFF_INITIAL
        for ran_for in (0.5, 61.0, 190.0, 3600.0):
            b = _next_backoff(b, BOT_GATE, ran_for=ran_for)
            assert b >= AUTH_BACKOFF_INITIAL

    def test_healthy_stretch_then_transient_error_resets(self):
        assert _next_backoff(40.0, "stream EOF / reconnect", ran_for=900.0) == (
            NORMAL_BACKOFF_INITIAL
        )

    def test_transient_error_keeps_the_fast_schedule(self):
        assert _next_backoff(5.0, "ffmpeg: connection reset", ran_for=3.0) == 10.0
        assert _next_backoff(40.0, "ffmpeg: connection reset", ran_for=3.0) == (
            NORMAL_BACKOFF_MAX
        )

    def test_auth_backoff_outranks_the_normal_cap(self):
        """A source already on the normal schedule that then hits a gate jumps
        to the auth schedule rather than staying capped at 60 s."""
        assert _next_backoff(NORMAL_BACKOFF_MAX, BOT_GATE, ran_for=190.0) == (
            AUTH_BACKOFF_INITIAL
        )


class TestCookieRefreshDebounce:
    def _stub(self, tmp_path: Path, monkeypatch, gated: list[str]):
        """A refresh() call wired to a temp cookies file, a bot-gated source
        list, and a yt-dlp that always fails the way a blocked IP does."""
        from birdbrain import cookies as mod  # noqa: PLC0415

        cookies_file = tmp_path / "www.youtube.com_cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n" + "x" * 2000)
        profile = tmp_path / "profile"
        profile.mkdir()

        # These tests are about the debounce, which only runs when the pinned
        # player client takes cookies at all. Pin that on so the suite keeps
        # covering the debounce after the client rotates to an anonymous one.
        monkeypatch.setattr(mod, "PLAYER_CLIENT_ACCEPTS_COOKIES", True)
        monkeypatch.setattr(
            mod, "_youtube_targets", lambda cfg, db: (cookies_file, "https://y/watch?v=a")
        )
        monkeypatch.setattr(mod, "bot_gated_sources", lambda db: list(gated))
        monkeypatch.setattr(mod, "find_firefox_profile", lambda: profile)

        calls: list[int] = []

        class _Res:
            returncode = 1
            stdout = ""
            stderr = "ERROR: [youtube] a: Sign in to confirm you're not a bot."

        def _run(cmd, **kw):
            calls.append(1)
            return _Res()

        monkeypatch.setattr(mod.subprocess, "run", _run)
        return mod, cookies_file, calls

    def test_failed_refresh_is_debounced(self, tmp_path, monkeypatch):
        """THE regression. The stamp used to be written only on success, so a
        refresh that kept failing was never debounced and the 5-minute timer
        re-exported and re-probed forever."""
        mod, _cookies, calls = self._stub(tmp_path, monkeypatch, ["Tembe"])

        first = mod.refresh(cfg=None, db=None)
        assert first["action"] == "failed"
        assert len(calls) == 1

        second = mod.refresh(cfg=None, db=None)
        assert second["action"] == "skip"
        assert second["reason"] == "debounced"
        assert len(calls) == 1, "a failed refresh must not re-probe immediately"

    def test_failure_stamp_expires_with_the_interval(self, tmp_path, monkeypatch):
        mod, cookies_file, calls = self._stub(tmp_path, monkeypatch, ["Tembe"])
        mod.refresh(cfg=None, db=None)

        stamp = cookies_file.with_suffix(cookies_file.suffix + ".refreshed")
        old = time.time() - 7 * 3600
        os.utime(stamp, (old, old))

        mod.refresh(cfg=None, db=None)
        assert len(calls) == 2, "past min_interval_h it should try again"

    def test_force_bypasses_the_failure_debounce(self, tmp_path, monkeypatch):
        mod, _, calls = self._stub(tmp_path, monkeypatch, ["Tembe"])
        mod.refresh(cfg=None, db=None)
        mod.refresh(cfg=None, db=None, force=True)
        assert len(calls) == 2

    def test_no_gated_cams_means_no_probe(self, tmp_path, monkeypatch):
        mod, _, calls = self._stub(tmp_path, monkeypatch, [])
        assert mod.refresh(cfg=None, db=None)["action"] == "skip"
        assert calls == []

    def test_anonymous_player_client_stands_down(self, tmp_path, monkeypatch):
        """When the pinned player client is refused *with* cookies, no export
        can help — refresh must not probe, even under force."""
        mod, _, calls = self._stub(tmp_path, monkeypatch, ["Tembe"])
        monkeypatch.setattr(mod, "PLAYER_CLIENT_ACCEPTS_COOKIES", False)

        res = mod.refresh(cfg=None, db=None, force=True)
        assert res["action"] == "skip"
        assert res["reason"] == "player client resolves without cookies"
        assert calls == []
