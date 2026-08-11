"""The supervisor watchdog's ability to notice a wedged worker.

Regression cover for a source that went dark for 13 hours and could not recover
on its own (JHB - Hyde Park, 2026-08-10). Two mechanisms are supposed to catch a
stuck worker and each missed it from a different side:

  * kick_stale() asks stale_workers() who has gone silent — but that query
    required ``state == 'running'``, and the row said ``stopped``.
  * reconcile() respawns a worker whose thread has died — but the thread was
    alive, wedged holding an ffmpeg that nothing was reading.

A worker writing ``stopped`` as it exits can land after its replacement has
started, which produces exactly that row: stopped, stale, and still held. These
tests pin the query on heartbeat age alone so the hole stays shut.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from birdbrain.storage import Database, WorkerHeartbeatRow


def _db(tmp_path) -> Database:
    return Database(f"sqlite:///{tmp_path / 'c.sqlite'}")


def _age(db: Database, name: str, seconds: float, state: str) -> None:
    """Create a heartbeat row, then backdate it to a given age and state.

    The row is created through the public API and only the two fields under
    test are adjusted directly — there is deliberately no "pretend this
    heartbeat is old" setter in Database.
    """
    db.worker_heartbeat(name)
    with db.session() as s:
        row = s.get(WorkerHeartbeatRow, name)
        row.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=seconds)
        row.state = state
        s.commit()


def test_stale_worker_is_found_regardless_of_state(tmp_path):
    """The bug: a stale row marked 'stopped' was invisible to the watchdog.

    'stopped' is the state a worker writes on its way out, and that write can
    land after a replacement is already running — so 'stopped' is precisely the
    state a stranded source ends up in.
    """
    db = _db(tmp_path)
    _age(db, "running-and-stale", 600, "running")
    _age(db, "stopped-and-stale", 600, "stopped")
    _age(db, "stalled-and-stale", 600, "stalled")

    stale = set(db.stale_workers(180))
    assert "running-and-stale" in stale       # always worked
    assert "stopped-and-stale" in stale       # the regression
    assert "stalled-and-stale" in stale       # a re-wedged replacement


def test_fresh_workers_are_never_stale(tmp_path):
    """Age is the only criterion, so a live worker must not be kicked whatever
    its state string says."""
    db = _db(tmp_path)
    _age(db, "fresh-running", 5, "running")
    _age(db, "fresh-stopped", 5, "stopped")
    assert db.stale_workers(180) == []


def test_retired_sources_are_reported_but_not_the_callers_problem(tmp_path):
    """Dropping the state filter means long-dead sources are named every tick.

    That is intended: kick_stale() skips any name it has no worker for, so the
    filtering belongs there (where the registry is known) and not in the query.
    This test documents the contract the caller relies on — if it ever changes,
    kick_stale()'s registry lookup has to change with it.
    """
    db = _db(tmp_path)
    _age(db, "retired-years-ago", 86_400 * 30, "stopped")
    assert "retired-years-ago" in db.stale_workers(180)

    # What the supervisor actually does with it: nothing, because it holds no
    # worker under that name.
    workers: dict[str, object] = {"something-else": object()}
    kicked = [n for n in db.stale_workers(180) if workers.get(n) is not None]
    assert kicked == []
