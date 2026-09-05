"""The start stagger must not put a worker inside the watchdog's sights.

Two mechanisms that are each correct alone and wrong together:

  * ``run_source`` delays its first yt-dlp resolve by ``index *
    WORKER_START_STAGGER_S`` so a cold start doesn't fire every resolve at
    once and trip YouTube's bot block.
  * ``kick_stale`` kills any worker whose heartbeat is older than
    STALE_HEARTBEAT_S, on the assumption that silence means a wedged read.

``worker_started`` stamps a heartbeat and then nothing touches it until the
first chunk arrives, so a starting worker's heartbeat age is its stagger plus
the resolve. At 11 sources the last slot waited 60s and the pipeline's own
comment called that "well under" the 180s threshold. At 20 it waits 114s, and
the resolve carries it over — so the watchdog kicks a worker that was waiting
its turn, reconcile() respawns it at a fresh index, and the respawn issues
another resolve. The mechanism that exists to spread resolves out starts
manufacturing them, in a loop, during the cold start it was meant to protect.

Seen on a 20-cam node (2026-09-05): the five highest-index sources were kicked
in index order while the account was already bot-gated.

These tests pin the fix — a waiting worker heartbeats — rather than a
particular roster size, because the size is what drifted.
"""

from __future__ import annotations

import threading
import time

from birdbrain.config import AppConfig
from birdbrain.pipeline import (
    STALE_HEARTBEAT_S,
    WORKER_START_STAGGER_S,
    _wait_out_start_delay,
)
from birdbrain.storage import Database


def _db(tmp_path) -> Database:
    return Database(f"sqlite:///{tmp_path / 'stagger.sqlite'}")


def _app(**kw) -> AppConfig:
    return AppConfig(**kw)


class _Log:
    def exception(self, *a, **k):  # pragma: no cover - only on a DB failure
        pass


def test_waiting_worker_heartbeats_so_the_watchdog_cannot_kick_it(tmp_path):
    """The regression. A worker serving its stagger must stay fresh."""
    db = _db(tmp_path)
    db.worker_started("Late Cam")
    # A tiny interval so the test runs fast; the real one is
    # app.worker_heartbeat_seconds.
    app = _app(worker_heartbeat_seconds=1.0)
    stopped = _wait_out_start_delay(db, app, "Late Cam", threading.Event(), 3.0, _Log())
    assert stopped is False
    # Nothing has gone stale even at a threshold far tighter than the real one.
    assert "Late Cam" not in db.stale_workers(2.0)


def test_stop_event_still_interrupts_the_wait(tmp_path):
    """Heartbeating must not cost promptness on shutdown — a restart sets
    stop_event and should not wait out a two-minute stagger."""
    db = _db(tmp_path)
    db.worker_started("Late Cam")
    ev = threading.Event()
    ev.set()
    assert _wait_out_start_delay(db, _app(), "Late Cam", ev, 600.0, _Log()) is True


def test_a_genuinely_silent_worker_is_still_caught(tmp_path):
    """The watchdog must keep its meaning: heartbeating during startup must not
    turn into heartbeating on behalf of a wedged worker."""
    db = _db(tmp_path)
    db.worker_started("Wedged Cam")
    # No _wait_out_start_delay call — nothing refreshes it.
    time.sleep(1.1)
    assert "Wedged Cam" in db.stale_workers(1.0)


def test_stagger_span_outgrows_the_stale_threshold_on_a_real_roster():
    """Documents *why* the heartbeat is load-bearing rather than belt-and-braces.

    If this ever fails because the span fits again, the heartbeat is still
    correct — but the bug it fixes would no longer be reachable, and the next
    person should know that before deleting it.
    """
    span_at_11 = 10 * WORKER_START_STAGGER_S
    span_at_20 = 19 * WORKER_START_STAGGER_S
    assert span_at_11 < STALE_HEARTBEAT_S, "the original assumption held at 11"
    # 114s of stagger, then a yt-dlp resolve on top, against a 180s threshold.
    assert span_at_20 > STALE_HEARTBEAT_S / 2, (
        "at 20 sources the stagger alone eats over half the stale budget, "
        "so the resolve pushes the last workers past it"
    )


def test_delay_of_zero_is_a_no_op(tmp_path):
    """The first worker in a batch has no delay and must not pay for the loop."""
    db = _db(tmp_path)
    db.worker_started("First Cam")
    assert _wait_out_start_delay(db, _app(), "First Cam", threading.Event(), 0.0, _Log()) is False
