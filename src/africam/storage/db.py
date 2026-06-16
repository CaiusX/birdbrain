from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Integer, create_engine, func, or_, select
from sqlalchemy.orm import Session, aliased, sessionmaker

from africam.detector.birdnet import Detection
from africam.storage.models import (
    AnomalyEventRow,
    AppSettingRow,
    AudioQualityMetricRow,
    AudioQualitySampleRow,
    Base,
    DailyBriefRow,
    HighlightIntervalRow,
    PlaybackStateRow,
    DetectionRow,
    RuntimeSourceRow,
    SiteNoteRow,
    SourceDisableRow,
    SourceStateRow,
    SpeciesNoteRow,
    SpeciesSiteNoteRow,
    WeatherObservationRow,
    WorkerDowntimeRow,
    WorkerHeartbeatRow,
)

# Sentinel for "argument not supplied" — distinct from None, which means
# "explicitly clear this field."
_UNSET: Any = object()

# app_settings key for the site-wide detection floor (see AppSettingRow).
GLOBAL_MIN_CONFIDENCE_KEY = "global_min_confidence"
# app_settings key prefix for per-source detection-floor overrides. The full
# key is this prefix + the source name, so each source's override is its own
# row, polled by that source's worker. Applies to both file (sources.toml) and
# runtime sources uniformly, so editing a cutoff on /admin is live for any
# source without a worker restart.
SOURCE_MIN_CONFIDENCE_PREFIX = "source_min_confidence:"


class Database:
    def __init__(self, url: str) -> None:
        # Ensure parent dir exists for sqlite file URLs.
        if url.startswith("sqlite:///"):
            db_path = Path(url.removeprefix("sqlite:///"))
            db_path.parent.mkdir(parents=True, exist_ok=True)

        # ``timeout`` is the Python sqlite3 driver's busy-wait, applied to
        # every connection in the pool. With the pipeline writing detection
        # rows continuously, the backfill and the notes worker would
        # otherwise hit ``OperationalError: database is locked`` whenever
        # they collided with the inserter. 30 s is plenty for the
        # pipeline's per-chunk transactions (sub-second) to clear.
        connect_args = (
            {"timeout": 30}
            if url.startswith("sqlite:")
            else {}
        )
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        if url.startswith("sqlite:"):
            # WAL lets the live pipeline insert detections while readers/writers
            # work concurrently without "database is locked". These are
            # per-connection pragmas (except WAL, which is file-persistent), so
            # set them on EVERY pooled connection via a connect hook — not just
            # once at init, which left pooled connections on the SQLite
            # defaults. ``journal_size_limit`` is the important one: after a
            # checkpoint SQLite truncates the WAL back to this size instead of
            # leaving it at its high-water mark — without it the WAL had
            # ballooned to ~100 MB and slowed every read.
            from sqlalchemy import event

            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA journal_size_limit=16777216")  # 16 MB cap
                cur.close()
        self._Session = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self._migrate_in_place()

    def _migrate_in_place(self) -> None:
        """Apply lightweight column-add migrations for SQLite without alembic.

        Only handles ADD COLUMN — anything more invasive needs a real migration
        framework. Existing rows get NULL for the new columns, which is fine.
        """
        if not self.engine.dialect.name == "sqlite":
            return
        added: list[tuple[str, str, str]] = [
            ("detections", "site", "TEXT"),
            ("detections", "latitude", "REAL"),
            ("detections", "longitude", "REAL"),
            ("detections", "label", "TEXT"),
            ("detections", "labeled_at", "TIMESTAMP"),
            ("detections", "suggested_species", "TEXT"),
            ("detections", "sound_rating", "INTEGER"),
            ("detections", "audio_hash", "TEXT"),
            ("species_notes", "conservation_status", "TEXT"),
            ("species_notes", "min_confidence", "REAL"),
            ("species_notes", "generated_at", "TIMESTAMP"),
            ("species_notes", "generated_by", "TEXT"),
            ("species_notes", "evidence_signature", "TEXT"),
            ("species_notes", "detection_count_at_gen", "INTEGER"),
            ("species_notes", "image_url", "TEXT"),
            ("species_notes", "image_page_url", "TEXT"),
            ("species_notes", "range_map_url", "TEXT"),
            ("species_notes", "range_map_page_url", "TEXT"),
            ("species_notes", "range_tile_url", "TEXT"),
            ("species_notes", "range_tile_page_url", "TEXT"),
            ("species_notes", "media_fetched_at", "TIMESTAMP"),
            ("runtime_sources", "timezone", "TEXT DEFAULT 'UTC'"),
            ("audio_quality_metrics", "band_hz_low", "INTEGER"),
            ("audio_quality_metrics", "band_hz_high", "INTEGER"),
        ]
        # Indexes to create on existing tables. ``Base.metadata.create_all``
        # only creates indexes for tables it creates, so any index attached to
        # a pre-existing table needs an explicit CREATE INDEX IF NOT EXISTS
        # here. SQLite treats these as cheap no-ops when the index already
        # exists, so it's safe to leave them in indefinitely.
        added_indexes: list[tuple[str, str, str]] = [
            (
                "ix_detections_source_hash_time",
                "detections",
                "source_name, audio_hash, started_at",
            ),
        ]
        # Indexes that have been superseded by something better. Dropped here
        # so old deployments don't drag along a redundant index forever.
        dropped_indexes: list[str] = [
            # Replaced by ix_detections_source_hash_time, which covers the
            # same lookups AND the started_at< inequality the replay
            # predicate needs.
            "ix_detections_source_hash",
        ]
        with self.engine.begin() as conn:
            for table, col, ddl in added:
                existing = {
                    row[1]
                    for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
                }
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            for name, table, cols in added_indexes:
                conn.exec_driver_sql(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"
                )
            for name in dropped_indexes:
                conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
            # ANALYZE populates sqlite_stat1 so the query planner has real
            # cardinality data when choosing between candidate indexes.
            # Without it the replay predicate would still latch onto
            # ix_detections_source_time for the started_at< inequality even
            # though ix_detections_source_hash_time is the better choice.
            # Cheap (~100 ms on this DB) and runs once per process boot.
            conn.exec_driver_sql("ANALYZE detections")

    def session(self) -> Session:
        return self._Session()

    def insert_detections(
        self,
        detections: Iterable[Detection],
        clip_path: str | None = None,
        site: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        audio_hash: str | None = None,
    ) -> int:
        rows = [
            DetectionRow(
                source_name=d.source_name,
                started_at=d.started_at,
                duration_s=d.duration_s,
                scientific_name=d.scientific_name,
                common_name=d.common_name,
                confidence=d.confidence,
                clip_path=clip_path,
                site=site,
                latitude=latitude,
                longitude=longitude,
                audio_hash=audio_hash,
            )
            for d in detections
        ]
        if not rows:
            return 0
        with self._Session() as s, s.begin():
            s.add_all(rows)
        return len(rows)

    # --- Replay filter: hides detections whose audio_hash already appeared
    # earlier on the same source. The hash itself is computed by
    # africam.audio_hash at insert time (and backfilled via
    # `africam dedup-backfill`). Rows with NULL hash are NEVER flagged as
    # replays — they're treated as "not yet evaluated."

    @staticmethod
    def not_replay_predicate():
        """Return a SQLAlchemy predicate that's True for non-replay rows.

        Compose into any ``select(DetectionRow)`` to hide replays::

            stmt = select(DetectionRow).where(Database.not_replay_predicate())

        Implementation: NOT EXISTS (earlier row at same source with same
        non-null hash). ``.correlate(DetectionRow)`` is critical — without
        it SQLAlchemy puts a fresh ``detections`` table reference in the
        subquery's FROM clause instead of correlating to the outer query,
        which silently turns the predicate into a meaningless cross-join.
        The composite index ``ix_detections_source_hash`` keeps this fast.
        """
        earlier = aliased(DetectionRow, name="earlier")
        return ~(
            select(1)
            .select_from(earlier)
            .where(earlier.source_name == DetectionRow.source_name)
            .where(earlier.audio_hash == DetectionRow.audio_hash)
            .where(earlier.audio_hash.is_not(None))
            .where(earlier.started_at < DetectionRow.started_at)
            .correlate(DetectionRow)
            .exists()
        )

    def backfill_audio_hash(
        self,
        days: int,
        batch_size: int,
        hasher,
        progress_cb=None,
    ) -> dict[str, int]:
        """Stream NULL-hash detections (newest-first within the window) and
        update ``audio_hash`` for each clip. ``hasher`` is a callable that
        takes the clip path and returns a hex string or None.

        Returns ``{processed, hashed, missing_clip, missing_file, errors}``
        as a small report dict. Idempotent: rows that already have a hash
        are skipped, so a second invocation finds nothing to do.

        Plays nicely with the live pipeline writer:
          * Hashing (the slow step) happens OUTSIDE any DB transaction.
          * Each batch flushes with a single bulk ``UPDATE … WHERE id IN
            VALUES (?)`` so the write lock is held for milliseconds.
          * ``PRAGMA busy_timeout`` waits a few seconds if the pipeline
            happens to be mid-insert when the batch tries to flush.
        """
        from pathlib import Path

        cutoff = datetime.now(UTC) - timedelta(days=days)
        report = {
            "processed": 0,
            "hashed": 0,
            "missing_clip": 0,  # clip_path is NULL on the row
            "missing_file": 0,  # path set but file gone from disk
            "errors": 0,
        }
        with self._Session() as s:
            todo = list(s.execute(
                select(DetectionRow.id, DetectionRow.clip_path)
                .where(DetectionRow.audio_hash.is_(None))
                .where(DetectionRow.started_at >= cutoff)
                .order_by(DetectionRow.started_at.desc())
            ).all())

        for i in range(0, len(todo), batch_size):
            chunk = todo[i : i + batch_size]
            updates: list[tuple[int, str]] = []
            for det_id, clip_path in chunk:
                report["processed"] += 1
                if not clip_path:
                    report["missing_clip"] += 1
                    continue
                if not Path(clip_path).exists():
                    report["missing_file"] += 1
                    continue
                try:
                    h = hasher(clip_path)
                except Exception:
                    report["errors"] += 1
                    continue
                if h is None:
                    report["errors"] += 1
                    continue
                updates.append((det_id, h))
            if updates:
                # Short-lived write transaction (the engine-level 30 s
                # busy_timeout handles collisions with the live pipeline).
                with self.engine.begin() as conn:
                    conn.exec_driver_sql(
                        "UPDATE detections SET audio_hash = ? WHERE id = ?",
                        [(h, det_id) for det_id, h in updates],
                    )
                report["hashed"] += len(updates)
            if progress_cb is not None:
                progress_cb(report, total=len(todo))
        return report

    # --- Source state (current site for multi-site streams) ---

    def get_source_state(self, source_name: str) -> SourceStateRow | None:
        with self._Session() as s:
            return s.get(SourceStateRow, source_name)

    def set_source_state(
        self,
        source_name: str,
        *,
        site: str | None,
        latitude: float | None,
        longitude: float | None,
        detected_by: str,
        manual_until: datetime | None = None,
    ) -> SourceStateRow:
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(SourceStateRow, source_name)
            if row is None:
                row = SourceStateRow(source_name=source_name, updated_at=now)
                s.add(row)
            row.site = site
            row.latitude = latitude
            row.longitude = longitude
            row.detected_by = detected_by
            row.manual_until = manual_until
            row.updated_at = now
        return row

    def set_detection_label(
        self,
        detection_id: int,
        label: str | None,
        *,
        suggested: Any = _UNSET,
        sound_rating: Any = _UNSET,
    ) -> bool:
        """Set/clear the manual audition label on a detection. Returns False if
        the detection doesn't exist.

        ``suggested`` is the rater's free-text guess at the actual species. It
        is only meaningful when ``label`` is ``'bad'`` or ``'unsure'``; for any
        other label this method clears it automatically. Pass the sentinel
        default to leave the existing suggestion unchanged.

        ``sound_rating`` is an optional 1–5 clip sound-quality score, set from
        the review popup. It is independent of the label (a correct ID can
        still sound poor), so it is NOT cleared when the label changes — only
        written when explicitly passed. ``None`` clears it; the sentinel leaves
        it untouched. Values outside 1–5 raise."""
        if label is not None and label not in ("good", "bad", "unsure"):
            raise ValueError(f"invalid label: {label!r}")
        if sound_rating not in (_UNSET, None) and sound_rating not in (1, 2, 3, 4, 5):
            raise ValueError(f"invalid sound_rating: {sound_rating!r}")
        with self._Session() as s, s.begin():
            row = s.get(DetectionRow, detection_id)
            if row is None:
                return False
            row.label = label
            row.labeled_at = datetime.now(UTC) if label is not None else None
            if label not in ("bad", "unsure"):
                # 'good' / unreviewed never carries a suggestion.
                row.suggested_species = None
            elif suggested is not _UNSET:
                row.suggested_species = (suggested or None)
            if sound_rating is not _UNSET:
                row.sound_rating = sound_rating
        return True

    # --- Worker heartbeats (admin liveness view) ---

    # --- Downtime interval helpers (called inside heartbeat transactions) ---

    def _ensure_open_downtime(
        self, s, source_name: str, now: datetime, reason: str
    ) -> None:
        """Open an interval for ``source_name`` if no open one exists.
        Idempotent — safe to call on every backoff tick. Must be called
        inside an active session transaction."""
        existing = s.execute(
            select(WorkerDowntimeRow)
            .where(WorkerDowntimeRow.source_name == source_name)
            .where(WorkerDowntimeRow.ended_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            s.add(WorkerDowntimeRow(
                source_name=source_name,
                started_at=now,
                reason=(reason or "")[:512] or None,
            ))

    def _close_open_downtime(self, s, source_name: str, now: datetime) -> None:
        """Close the currently-open interval for ``source_name`` if any."""
        existing = s.execute(
            select(WorkerDowntimeRow)
            .where(WorkerDowntimeRow.source_name == source_name)
            .where(WorkerDowntimeRow.ended_at.is_(None))
            .order_by(WorkerDowntimeRow.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing.ended_at = now

    def worker_started(self, source_name: str) -> None:
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(WorkerHeartbeatRow, source_name)
            was_running = row is not None and row.state == "running"
            if row is None:
                row = WorkerHeartbeatRow(source_name=source_name)
                s.add(row)
            row.started_at = now
            row.last_heartbeat_at = now
            row.state = "running"
            row.last_error = None
            if not was_running:
                self._close_open_downtime(s, source_name, now)

    def worker_heartbeat(self, source_name: str) -> None:
        """Bump last_heartbeat_at. Called every ~15s while the worker is healthy."""
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(WorkerHeartbeatRow, source_name)
            was_running = row is not None and row.state == "running"
            if row is None:
                row = WorkerHeartbeatRow(
                    source_name=source_name, started_at=now, last_heartbeat_at=now
                )
                s.add(row)
            else:
                row.last_heartbeat_at = now
                row.state = "running"
                row.last_error = None
            if not was_running:
                self._close_open_downtime(s, source_name, now)

    def worker_backoff(self, source_name: str, error: str) -> None:
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(WorkerHeartbeatRow, source_name)
            if row is None:
                row = WorkerHeartbeatRow(
                    source_name=source_name, started_at=now, last_heartbeat_at=now
                )
                s.add(row)
            row.state = "backoff"
            row.last_error = (error or "")[:512] or None
            row.last_heartbeat_at = now
            # Idempotent — opens an interval iff none currently open. Note: if
            # the app starts up while a site is already down, the first backoff
            # call here will stamp started_at=NOW even though the real outage
            # may be older; that's a known accuracy limit, not a bug.
            self._ensure_open_downtime(s, source_name, now, error)

    # --- Downtime query methods (for /admin and /site/<name>) ---

    def current_downtime_by_source(self) -> dict[str, WorkerDowntimeRow]:
        """Map source_name → currently-open downtime row (for the admin
        table). Sites not in the dict are currently up (or have never been
        observed by this codebase yet)."""
        with self._Session() as s:
            rows = list(s.scalars(
                select(WorkerDowntimeRow)
                .where(WorkerDowntimeRow.ended_at.is_(None))
            ))
        return {r.source_name: r for r in rows}

    def downtime_seconds_since(
        self, source_name: str, since: datetime
    ) -> int:
        """Total seconds of downtime for this source in [since, now)."""
        now = datetime.now(UTC)
        with self._Session() as s:
            rows = s.execute(
                select(WorkerDowntimeRow.started_at, WorkerDowntimeRow.ended_at)
                .where(WorkerDowntimeRow.source_name == source_name)
                .where(
                    WorkerDowntimeRow.ended_at.is_(None)
                    | (WorkerDowntimeRow.ended_at > since)
                )
            ).all()
        total = 0
        for start, end in rows:
            s_utc = start if start.tzinfo else start.replace(tzinfo=UTC)
            e_utc = (
                end if (end is None or end.tzinfo) else end.replace(tzinfo=UTC)
            )
            effective_start = max(s_utc, since)
            effective_end = e_utc if e_utc is not None else now
            if effective_end > effective_start:
                total += int((effective_end - effective_start).total_seconds())
        return total

    # --- Highlight playback state (written by HighlightWatcher) ---

    def _ensure_open_highlight(self, s, source_name: str, now: datetime) -> None:
        existing = s.execute(
            select(HighlightIntervalRow)
            .where(HighlightIntervalRow.source_name == source_name)
            .where(HighlightIntervalRow.ended_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            s.add(HighlightIntervalRow(source_name=source_name, started_at=now))

    def _close_open_highlight(self, s, source_name: str, now: datetime) -> None:
        existing = s.execute(
            select(HighlightIntervalRow)
            .where(HighlightIntervalRow.source_name == source_name)
            .where(HighlightIntervalRow.ended_at.is_(None))
            .order_by(HighlightIntervalRow.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing.ended_at = now

    def record_highlight_state(self, source_name: str, in_highlight: bool) -> None:
        """Persist the watcher's current reading. Always bumps ``checked_at``
        (freshness); on a state change, stamps ``since`` and opens/closes the
        matching highlight interval. Called every watcher tick."""
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(PlaybackStateRow, source_name)
            changed = row is None or row.in_highlight != in_highlight
            if row is None:
                row = PlaybackStateRow(
                    source_name=source_name,
                    in_highlight=in_highlight,
                    since=now,
                    checked_at=now,
                )
                s.add(row)
            else:
                if changed:
                    row.in_highlight = in_highlight
                    row.since = now
                row.checked_at = now
            if changed:
                if in_highlight:
                    self._ensure_open_highlight(s, source_name, now)
                else:
                    self._close_open_highlight(s, source_name, now)

    def playback_state_by_source(self) -> dict[str, PlaybackStateRow]:
        """Map source_name → its current playback-state row (for /admin)."""
        with self._Session() as s:
            return {r.source_name: r for r in s.scalars(select(PlaybackStateRow))}

    # --- Audio quality metric (written by the pipeline, read by the UI) ---

    def upsert_audio_quality(self, source_name: str, snap: dict) -> None:
        """Write a source's current audio-quality snapshot (the dict returned
        by QualityAccumulator.snapshot()). Called ~every 60s per worker."""
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(AudioQualityMetricRow, source_name)
            if row is None:
                row = AudioQualityMetricRow(source_name=source_name)
                s.add(row)
            row.score = int(snap["score"])
            row.level_score = float(snap["level_score"])
            row.avail_score = float(snap["avail_score"])
            row.structure_score = float(snap["structure_score"])
            row.level_dbfs = float(snap["level_dbfs"])
            row.silence_fraction = float(snap["silence_fraction"])
            row.clip_fraction = float(snap["clip_fraction"])
            row.flatness = float(snap["flatness"])
            row.fraction_good = float(snap["fraction_good"])
            row.issue_label = str(snap["issue_label"])[:32]
            row.band_hz_low = snap.get("band_hz_low")
            row.band_hz_high = snap.get("band_hz_high")
            row.updated_at = now

    def audio_quality_by_source(self) -> dict[str, AudioQualityMetricRow]:
        """Map source_name → its current audio-quality row (for /admin)."""
        with self._Session() as s:
            return {r.source_name: r for r in s.scalars(select(AudioQualityMetricRow))}

    def detection_count_since(self, source_name: str, since: datetime) -> int:
        """Raw detection count for a source in [since, now). Feeds the audio-
        quality 'yield' term (loud audio + near-zero detections = masked)."""
        with self._Session() as s:
            return int(s.scalar(
                select(func.count(DetectionRow.id))
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at >= since)
            ) or 0)

    def append_audio_quality_sample(
        self, source_name: str, score: int, level_dbfs: float, structure_score: float
    ) -> None:
        """Append one point to the 24h-trend time-series (~every 10 min)."""
        with self._Session() as s, s.begin():
            s.add(AudioQualitySampleRow(
                source_name=source_name,
                recorded_at=datetime.now(UTC),
                score=int(score),
                level_dbfs=float(level_dbfs),
                structure_score=float(structure_score),
            ))

    def audio_quality_samples_since(
        self, source_name: str, since: datetime
    ) -> list[tuple[datetime, int]]:
        """(recorded_at, score) points for a source in [since, now), oldest
        first — feeds the site-page trend sparkline."""
        with self._Session() as s:
            rows = s.execute(
                select(AudioQualitySampleRow.recorded_at, AudioQualitySampleRow.score)
                .where(AudioQualitySampleRow.source_name == source_name)
                .where(AudioQualitySampleRow.recorded_at >= since)
                .order_by(AudioQualitySampleRow.recorded_at)
            ).all()
        return [(r[0], r[1]) for r in rows]

    def prune_audio_quality_samples(self, older_than: datetime) -> int:
        """Delete trend samples older than ``older_than``. Returns row count."""
        from sqlalchemy import delete
        with self._Session() as s, s.begin():
            res = s.execute(
                delete(AudioQualitySampleRow)
                .where(AudioQualitySampleRow.recorded_at < older_than)
            )
        return res.rowcount or 0

    def clear_playback_state(self, source_name: str) -> None:
        """Forget a source's playback state and close any open highlight
        interval. Called when gating is switched off so /admin stops showing a
        live/highlight badge promptly instead of waiting for staleness."""
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(PlaybackStateRow, source_name)
            if row is not None:
                s.delete(row)
            self._close_open_highlight(s, source_name, now)

    def highlight_seconds_since(self, source_name: str, since: datetime) -> int:
        """Total seconds this source spent in highlights in [since, now)."""
        now = datetime.now(UTC)
        with self._Session() as s:
            rows = s.execute(
                select(HighlightIntervalRow.started_at, HighlightIntervalRow.ended_at)
                .where(HighlightIntervalRow.source_name == source_name)
                .where(
                    HighlightIntervalRow.ended_at.is_(None)
                    | (HighlightIntervalRow.ended_at > since)
                )
            ).all()
        total = 0
        for start, end in rows:
            s_utc = start if start.tzinfo else start.replace(tzinfo=UTC)
            e_utc = end if (end is None or end.tzinfo) else end.replace(tzinfo=UTC)
            effective_start = max(s_utc, since)
            effective_end = e_utc if e_utc is not None else now
            if effective_end > effective_start:
                total += int((effective_end - effective_start).total_seconds())
        return total

    def recent_downtime(
        self, source_name: str, limit: int = 5
    ) -> list[WorkerDowntimeRow]:
        """Most-recent CLOSED outages for the per-site uptime panel."""
        with self._Session() as s:
            return list(s.scalars(
                select(WorkerDowntimeRow)
                .where(WorkerDowntimeRow.source_name == source_name)
                .where(WorkerDowntimeRow.ended_at.is_not(None))
                .order_by(WorkerDowntimeRow.started_at.desc())
                .limit(limit)
            ))

    def worker_stopped(self, source_name: str) -> None:
        with self._Session() as s, s.begin():
            row = s.get(WorkerHeartbeatRow, source_name)
            if row is not None:
                row.state = "stopped"

    def list_worker_heartbeats(self) -> list[WorkerHeartbeatRow]:
        with self._Session() as s:
            return list(s.scalars(select(WorkerHeartbeatRow)))

    def stale_workers(self, max_age_s: float) -> list[str]:
        """Source names whose worker still claims to be running but hasn't
        sent a heartbeat in ``max_age_s`` seconds. These are stuck —
        typically blocked on an ffmpeg read against a frozen HLS stream.
        The supervisor uses this to kick replacements."""
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_s)
        with self._Session() as s:
            rows = s.scalars(
                select(WorkerHeartbeatRow)
                .where(WorkerHeartbeatRow.state == "running")
                .where(WorkerHeartbeatRow.last_heartbeat_at < cutoff)
            )
            return [r.source_name for r in rows]

    def worker_stalled(self, source_name: str, error: str = "") -> None:
        """Mark a worker as stalled. Set by the supervisor watchdog when it
        kicks a worker whose heartbeat went silent."""
        with self._Session() as s, s.begin():
            row = s.get(WorkerHeartbeatRow, source_name)
            if row is not None:
                row.state = "stalled"
                row.last_error = (error or "no heartbeat — supervisor restarting")[:512]

    # --- Species notes (curated commentary shown in audition) ---

    def get_species_note(self, scientific_name: str) -> SpeciesNoteRow | None:
        with self._Session() as s:
            return s.get(SpeciesNoteRow, scientific_name)

    def set_species_note(
        self,
        scientific_name: str,
        *,
        common_name: str = "",
        note: str,
        tag: str | None = None,
    ) -> SpeciesNoteRow:
        if tag is not None and tag not in ("reliable", "suspect", "rare"):
            raise ValueError(f"invalid tag: {tag!r}")
        with self._Session() as s, s.begin():
            row = s.get(SpeciesNoteRow, scientific_name)
            if row is None:
                row = SpeciesNoteRow(scientific_name=scientific_name)
                s.add(row)
            if common_name:
                row.common_name = common_name
            row.note = note
            row.tag = tag
            row.updated_at = datetime.now(UTC)
        return row

    def set_species_status(self, scientific_name: str, status: str | None) -> None:
        """Cache a Wikipedia-derived IUCN status code on the species row.
        Creates a minimal note row if one doesn't already exist so we have
        somewhere to hang the status — the curated note stays NULL/empty."""
        if status and len(status) > 8:
            return
        with self._Session() as s, s.begin():
            row = s.get(SpeciesNoteRow, scientific_name)
            if row is None:
                row = SpeciesNoteRow(
                    scientific_name=scientific_name,
                    common_name="",
                    note="",
                    updated_at=datetime.now(UTC),
                )
                s.add(row)
            row.conservation_status = status

    def set_species_media(
        self,
        scientific_name: str,
        *,
        common_name: str = "",
        image_url: str | None = None,
        image_page_url: str | None = None,
        range_map_url: str | None = None,
        range_map_page_url: str | None = None,
        range_tile_url: str | None = None,
        range_tile_page_url: str | None = None,
    ) -> None:
        """Cache Wikipedia photo + range-map URLs on the species row and stamp
        media_fetched_at. Creates a minimal note row if needed (mirrors
        set_species_status). Always stamps the timestamp — even when nothing was
        found — so the sweeper won't keep retrying a species that has no map."""
        with self._Session() as s, s.begin():
            row = s.get(SpeciesNoteRow, scientific_name)
            if row is None:
                row = SpeciesNoteRow(
                    scientific_name=scientific_name,
                    common_name=common_name or "",
                    note="",
                    updated_at=datetime.now(UTC),
                )
                s.add(row)
            elif common_name and not row.common_name:
                row.common_name = common_name
            row.image_url = image_url
            row.image_page_url = image_page_url
            row.range_map_url = range_map_url
            row.range_map_page_url = range_map_page_url
            row.range_tile_url = range_tile_url
            row.range_tile_page_url = range_tile_page_url
            row.media_fetched_at = datetime.now(UTC)

    def species_needing_media(
        self, *, limit: int = 25, max_age_days: int = 30
    ) -> list[tuple[str, str]]:
        """Species seen in detections whose cached media is missing or stale —
        (scientific_name, common_name) pairs for the background sweeper to fetch.
        Covers new species automatically (no note row → media_fetched_at NULL)."""
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        with self._Session() as s:
            seen = s.execute(
                select(
                    DetectionRow.scientific_name,
                    func.max(DetectionRow.common_name),
                ).group_by(DetectionRow.scientific_name)
            ).all()
            fetched = dict(
                s.execute(
                    select(
                        SpeciesNoteRow.scientific_name,
                        SpeciesNoteRow.media_fetched_at,
                    )
                ).all()
            )
        out: list[tuple[str, str]] = []
        for sci, common in seen:
            ts = fetched.get(sci)
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts is None or ts < cutoff:
                out.append((sci, common or ""))
            if len(out) >= limit:
                break
        return out

    def delete_species_note(self, scientific_name: str) -> bool:
        with self._Session() as s, s.begin():
            row = s.get(SpeciesNoteRow, scientific_name)
            if row is None:
                return False
            s.delete(row)
        return True

    def species_min_confidence_map(self) -> dict[str, float]:
        """Map of scientific_name → per-species minimum confidence override
        (only for species that have one set). The pipeline polls this so
        workers can suppress loud common species without restarting."""
        with self._Session() as s:
            rows = s.execute(
                select(SpeciesNoteRow.scientific_name, SpeciesNoteRow.min_confidence)
                .where(SpeciesNoteRow.min_confidence.is_not(None))
            ).all()
        return {sci: float(mc) for sci, mc in rows}

    def set_species_min_confidence(
        self, scientific_name: str, value: float | None
    ) -> None:
        """Set/clear the per-species detection floor. Creates a minimal note
        row if none exists so we have somewhere to hang the override."""
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError("min_confidence must be in [0, 1]")
        with self._Session() as s, s.begin():
            row = s.get(SpeciesNoteRow, scientific_name)
            if row is None:
                row = SpeciesNoteRow(
                    scientific_name=scientific_name,
                    common_name="",
                    note="",
                    updated_at=datetime.now(UTC),
                )
                s.add(row)
            row.min_confidence = value

    # --- App-wide settings (key/value, polled cross-process) ---

    def get_setting(self, key: str) -> str | None:
        """Raw value for an app_settings key, or None if unset."""
        with self._Session() as s:
            row = s.get(AppSettingRow, key)
            return row.value if row is not None else None

    def list_settings_with_prefix(self, prefix: str) -> dict[str, str]:
        """All app_settings rows whose key starts with ``prefix`` (non-null
        values only), keyed by full key. Used to read per-source settings in
        one query."""
        with self._Session() as s:
            rows = s.execute(
                select(AppSettingRow.key, AppSettingRow.value)
                .where(AppSettingRow.key.like(f"{prefix}%"))
                .where(AppSettingRow.value.is_not(None))
            ).all()
        return {k: v for k, v in rows}

    def set_setting(self, key: str, value: str | None) -> None:
        """Upsert an app_settings value; ``None`` deletes the row (= unset)."""
        with self._Session() as s, s.begin():
            row = s.get(AppSettingRow, key)
            if value is None:
                if row is not None:
                    s.delete(row)
                return
            if row is None:
                row = AppSettingRow(key=key)
                s.add(row)
            row.value = value
            row.updated_at = datetime.now(UTC)

    def global_min_confidence(self) -> float | None:
        """Site-wide detection floor, or None when no override is set. The
        pipeline polls this (like the per-species floors) so a change in the
        web UI lands on every worker within the refresh interval."""
        raw = self.get_setting(GLOBAL_MIN_CONFIDENCE_KEY)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def set_global_min_confidence(self, value: float | None) -> None:
        """Set/clear the site-wide detection floor. ``None`` clears it,
        restoring each source's own ``min_confidence``."""
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError("min_confidence must be in [0, 1]")
        self.set_setting(
            GLOBAL_MIN_CONFIDENCE_KEY,
            None if value is None else repr(float(value)),
        )

    def source_min_confidence(self, source_name: str) -> float | None:
        """Per-source detection-floor override for one source, or None when
        unset. A worker polls its own source's key so an /admin edit lands
        without a restart."""
        raw = self.get_setting(SOURCE_MIN_CONFIDENCE_PREFIX + source_name)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def source_min_confidence_map(self) -> dict[str, float]:
        """All per-source overrides, keyed by source name. Used by the web UI
        to show which sources carry an override."""
        out: dict[str, float] = {}
        with self._Session() as s:
            rows = s.execute(
                select(AppSettingRow.key, AppSettingRow.value)
                .where(AppSettingRow.key.like(f"{SOURCE_MIN_CONFIDENCE_PREFIX}%"))
                .where(AppSettingRow.value.is_not(None))
            ).all()
        for key, value in rows:
            try:
                out[key[len(SOURCE_MIN_CONFIDENCE_PREFIX):]] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def set_source_min_confidence(
        self, source_name: str, value: float | None
    ) -> None:
        """Set/clear a per-source detection-floor override. ``None`` clears it,
        restoring the source's configured ``min_confidence``."""
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError("min_confidence must be in [0, 1]")
        self.set_setting(
            SOURCE_MIN_CONFIDENCE_PREFIX + source_name,
            None if value is None else repr(float(value)),
        )

    def list_detected_species(self) -> list[tuple[str, str]]:
        """Distinct (scientific_name, common_name) ever detected, ordered by
        common name. Feeds the species picker on the admin cutoffs panel."""
        with self._Session() as s:
            rows = s.execute(
                select(
                    DetectionRow.scientific_name,
                    func.min(DetectionRow.common_name),
                )
                .group_by(DetectionRow.scientific_name)
                .order_by(func.min(DetectionRow.common_name))
            ).all()
        return [(sci, common or sci) for sci, common in rows]

    def list_species_notes(self) -> list[SpeciesNoteRow]:
        with self._Session() as s:
            return list(s.scalars(select(SpeciesNoteRow)))

    # --- AI-generated species notes (background worker) ---

    def gather_species_evidence(self, scientific_name: str) -> dict | None:
        """Aggregate this species' detection footprint for the notes worker.

        Returns None when there's no data to summarize. The shape is the
        contract between the DB and the Claude prompt — change with care
        (and bump the evidence_signature inputs accordingly)."""
        from sqlalchemy import func

        with self._Session() as s:
            common = s.scalar(
                select(DetectionRow.common_name)
                .where(DetectionRow.scientific_name == scientific_name)
                .order_by(DetectionRow.started_at.desc())
                .limit(1)
            )
            if common is None:
                return None

            count, mean_conf, max_conf, first_seen, last_seen = s.execute(
                select(
                    func.count(DetectionRow.id),
                    func.avg(DetectionRow.confidence),
                    func.max(DetectionRow.confidence),
                    func.min(DetectionRow.started_at),
                    func.max(DetectionRow.started_at),
                ).where(DetectionRow.scientific_name == scientific_name)
            ).one()

            per_source = s.execute(
                select(DetectionRow.source_name, func.count(DetectionRow.id))
                .where(DetectionRow.scientific_name == scientific_name)
                .group_by(DetectionRow.source_name)
                .order_by(func.count(DetectionRow.id).desc())
            ).all()

            # SQLite stores started_at without tz info; strftime works on the
            # local-clock string. Since we always write UTC, this gives UTC hour.
            per_hour = s.execute(
                select(
                    func.strftime("%H", DetectionRow.started_at),
                    func.count(DetectionRow.id),
                )
                .where(DetectionRow.scientific_name == scientific_name)
                .group_by(func.strftime("%H", DetectionRow.started_at))
            ).all()

            label_counts = dict(s.execute(
                select(DetectionRow.label, func.count(DetectionRow.id))
                .where(DetectionRow.scientific_name == scientific_name)
                .where(DetectionRow.label.is_not(None))
                .group_by(DetectionRow.label)
            ).all())

            top_clips = s.execute(
                select(
                    DetectionRow.confidence,
                    DetectionRow.source_name,
                    DetectionRow.started_at,
                )
                .where(DetectionRow.scientific_name == scientific_name)
                .where(DetectionRow.clip_path.is_not(None))
                .order_by(DetectionRow.confidence.desc())
                .limit(3)
            ).all()

            note_row = s.get(SpeciesNoteRow, scientific_name)

        hourly = [0] * 24
        for hr_str, c in per_hour:
            if hr_str is not None:
                hourly[int(hr_str)] = c

        return {
            "scientific_name": scientific_name,
            "common_name": common,
            "detection_count": int(count or 0),
            "mean_confidence": round(float(mean_conf or 0), 3),
            "max_confidence": round(float(max_conf or 0), 3),
            "first_seen_utc": first_seen.isoformat() if first_seen else None,
            "last_seen_utc": last_seen.isoformat() if last_seen else None,
            "per_source": [{"source": n, "count": int(c)} for n, c in per_source],
            "hourly_utc": hourly,
            "audition_labels": {k: int(v) for k, v in label_counts.items()},
            "top_clips_utc": [
                {
                    "confidence": round(float(conf), 3),
                    "source": src,
                    "at_utc": at.isoformat(),
                }
                for conf, src, at in top_clips
            ],
            "conservation_status": note_row.conservation_status if note_row else None,
        }

    def pick_stale_species_for_note(
        self,
        *,
        max_age_days: int,
        min_detections: int,
        regen_count_factor: float = 2.0,
    ) -> str | None:
        """Return the scientific_name most overdue for a (re)generated note.

        Priority order:
          1. Species with detections but no AI note yet (oldest first)
          2. Species whose detection count has grown by ``regen_count_factor``
             since the last generation (most-grown first)
          3. Species whose note is older than ``max_age_days`` (oldest first)

        Skips rows whose note is curated (note non-empty AND generated_at NULL)
        so manual edits aren't overwritten.
        """
        from sqlalchemy import func

        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        with self._Session() as s:
            counts_subq = (
                select(
                    DetectionRow.scientific_name.label("sci"),
                    DetectionRow.common_name.label("common"),
                    func.count(DetectionRow.id).label("cnt"),
                )
                .group_by(DetectionRow.scientific_name)
                .having(func.count(DetectionRow.id) >= min_detections)
                .subquery()
            )

            # 1. No AI note yet (and no curated note either) — these have
            #    never been written.
            missing = s.execute(
                select(counts_subq.c.sci)
                .outerjoin(
                    SpeciesNoteRow,
                    SpeciesNoteRow.scientific_name == counts_subq.c.sci,
                )
                .where(
                    (SpeciesNoteRow.scientific_name.is_(None))
                    | ((SpeciesNoteRow.generated_at.is_(None)) & (SpeciesNoteRow.note == ""))
                )
                .order_by(counts_subq.c.cnt.desc())
                .limit(1)
            ).scalar()
            if missing:
                return missing

            # 2. Significant growth since last generation.
            grown = s.execute(
                select(counts_subq.c.sci)
                .join(
                    SpeciesNoteRow,
                    SpeciesNoteRow.scientific_name == counts_subq.c.sci,
                )
                .where(SpeciesNoteRow.generated_at.is_not(None))
                .where(SpeciesNoteRow.detection_count_at_gen.is_not(None))
                .where(
                    counts_subq.c.cnt
                    >= SpeciesNoteRow.detection_count_at_gen * regen_count_factor
                )
                .order_by(
                    (counts_subq.c.cnt - SpeciesNoteRow.detection_count_at_gen).desc()
                )
                .limit(1)
            ).scalar()
            if grown:
                return grown

            # 3. Aged out.
            aged = s.execute(
                select(counts_subq.c.sci)
                .join(
                    SpeciesNoteRow,
                    SpeciesNoteRow.scientific_name == counts_subq.c.sci,
                )
                .where(SpeciesNoteRow.generated_at.is_not(None))
                .where(SpeciesNoteRow.generated_at < cutoff)
                .order_by(SpeciesNoteRow.generated_at.asc())
                .limit(1)
            ).scalar()
            return aged

    def set_generated_species_note(
        self,
        scientific_name: str,
        *,
        common_name: str,
        note: str,
        generated_by: str,
        evidence_signature: str,
        detection_count_at_gen: int,
    ) -> None:
        """Write an AI-generated note. Preserves any manually-set tag and
        conservation_status on the existing row."""
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(SpeciesNoteRow, scientific_name)
            if row is None:
                row = SpeciesNoteRow(scientific_name=scientific_name)
                s.add(row)
            if common_name:
                row.common_name = common_name
            row.note = note
            row.updated_at = now
            row.generated_at = now
            row.generated_by = generated_by
            row.evidence_signature = evidence_signature
            row.detection_count_at_gen = detection_count_at_gen

    # --- Runtime source enable (undo soft-delete) ---

    def enable_runtime_source(self, name: str) -> bool:
        with self._Session() as s, s.begin():
            row = s.get(RuntimeSourceRow, name)
            if row is None:
                return False
            row.deleted_at = None
        return True

    def clear_manual_override(self, source_name: str) -> None:
        with self._Session() as s, s.begin():
            row = s.get(SourceStateRow, source_name)
            if row is not None:
                row.manual_until = None
                row.detected_by = None
                row.updated_at = datetime.now(UTC)

    # --- Runtime sources (added/removed via the web UI without restart) ---

    def add_runtime_source(self, **fields) -> RuntimeSourceRow:
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(RuntimeSourceRow, fields["name"])
            if row is None:
                row = RuntimeSourceRow(created_at=now)
                s.add(row)
            for k, v in fields.items():
                setattr(row, k, v)
            row.deleted_at = None
        return row

    def set_runtime_source_location(
        self, name: str, lat: float | None, lon: float | None
    ) -> bool:
        """Update just the lat/lon on an existing runtime source. Takes effect
        the next time the worker (re)starts — lat/lon are read into the worker's
        config at startup, not polled live. Returns False if no runtime row."""
        with self._Session() as s, s.begin():
            row = s.get(RuntimeSourceRow, name)
            if row is None:
                return False
            row.lat = lat
            row.lon = lon
        return True

    def soft_delete_runtime_source(self, name: str) -> bool:
        with self._Session() as s, s.begin():
            row = s.get(RuntimeSourceRow, name)
            if row is None or row.deleted_at is not None:
                return False
            row.deleted_at = datetime.now(UTC)
        return True

    def list_runtime_sources(self, *, include_deleted: bool = False) -> list[RuntimeSourceRow]:
        with self._Session() as s:
            stmt = select(RuntimeSourceRow).order_by(RuntimeSourceRow.name)
            if not include_deleted:
                stmt = stmt.where(RuntimeSourceRow.deleted_at.is_(None))
            return list(s.scalars(stmt))

    # --- Source-level disable overrides (works for static + runtime sources) ---

    def list_disabled_source_names(self) -> set[str]:
        """Source names currently flagged disabled by an admin toggle. Used
        by ``_all_sources`` to drop them from the active roster — works for
        BOTH file-managed (sources.toml) and runtime sources."""
        with self._Session() as s:
            return {
                r for (r,) in s.execute(select(SourceDisableRow.source_name))
            }

    def set_source_disabled(self, source_name: str, disabled: bool) -> None:
        """Flip the disable override for a source. Disabling a runtime source
        is redundant with ``disable_runtime_source`` (which sets deleted_at);
        the API endpoint dispatches correctly. For static sources this is the
        only mechanism."""
        with self._Session() as s, s.begin():
            row = s.get(SourceDisableRow, source_name)
            if disabled and row is None:
                s.add(SourceDisableRow(
                    source_name=source_name, disabled_at=datetime.now(UTC)
                ))
            elif not disabled and row is not None:
                s.delete(row)

    # --- Site-level evidence + AI notes (one per source_name) ---

    def gather_site_evidence(self, source_name: str) -> dict | None:
        """Aggregate this site's detection footprint for the notes worker.

        Counterpart to gather_species_evidence — same shape philosophy:
        deterministic JSON, bucketed for signature stability, enough
        structure for Claude to find the story without us pre-chewing it."""
        from sqlalchemy import func

        with self._Session() as s:
            count, mean_conf, max_conf, first_seen, last_seen, distinct_species = (
                s.execute(
                    select(
                        func.count(DetectionRow.id),
                        func.avg(DetectionRow.confidence),
                        func.max(DetectionRow.confidence),
                        func.min(DetectionRow.started_at),
                        func.max(DetectionRow.started_at),
                        func.count(func.distinct(DetectionRow.scientific_name)),
                    ).where(DetectionRow.source_name == source_name)
                ).one()
            )
            if not count:
                return None

            top_species = s.execute(
                select(
                    DetectionRow.scientific_name,
                    DetectionRow.common_name,
                    func.count(DetectionRow.id),
                    func.max(DetectionRow.confidence),
                )
                .where(DetectionRow.source_name == source_name)
                .group_by(DetectionRow.scientific_name)
                .order_by(func.count(DetectionRow.id).desc())
                .limit(10)
            ).all()

            per_hour = s.execute(
                select(
                    func.strftime("%H", DetectionRow.started_at),
                    func.count(DetectionRow.id),
                )
                .where(DetectionRow.source_name == source_name)
                .group_by(func.strftime("%H", DetectionRow.started_at))
            ).all()

            label_counts = dict(s.execute(
                select(DetectionRow.label, func.count(DetectionRow.id))
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.label.is_not(None))
                .group_by(DetectionRow.label)
            ).all())

            top_clips = s.execute(
                select(
                    DetectionRow.confidence,
                    DetectionRow.common_name,
                    DetectionRow.scientific_name,
                    DetectionRow.started_at,
                )
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.clip_path.is_not(None))
                .order_by(DetectionRow.confidence.desc())
                .limit(3)
            ).all()

        hourly = [0] * 24
        for hr_str, c in per_hour:
            if hr_str is not None:
                hourly[int(hr_str)] = c

        return {
            "source_name": source_name,
            "detection_count": int(count),
            "distinct_species": int(distinct_species or 0),
            "mean_confidence": round(float(mean_conf or 0), 3),
            "max_confidence": round(float(max_conf or 0), 3),
            "first_seen_utc": first_seen.isoformat() if first_seen else None,
            "last_seen_utc": last_seen.isoformat() if last_seen else None,
            "top_species": [
                {
                    "scientific_name": sci,
                    "common_name": com,
                    "count": int(c),
                    "max_confidence": round(float(mc), 3),
                }
                for sci, com, c, mc in top_species
            ],
            "hourly_utc": hourly,
            "audition_labels": {k: int(v) for k, v in label_counts.items()},
            "top_clips_utc": [
                {
                    "confidence": round(float(conf), 3),
                    "common_name": com,
                    "scientific_name": sci,
                    "at_utc": at.isoformat(),
                }
                for conf, com, sci, at in top_clips
            ],
        }

    def pick_stale_site_for_note(
        self,
        *,
        candidate_sources: Iterable[str],
        max_age_days: int,
        min_detections: int,
        regen_count_factor: float = 2.0,
    ) -> str | None:
        """Return the source_name most overdue for a site note, or None.

        Unlike species (where the candidate set is "any species ever
        detected"), the candidate set for sites is the small static list of
        sources from sources.toml — the worker passes it in. Priority order
        matches the species picker."""
        from sqlalchemy import func

        candidates = list(candidate_sources)
        if not candidates:
            return None

        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        with self._Session() as s:
            counts = dict(s.execute(
                select(DetectionRow.source_name, func.count(DetectionRow.id))
                .where(DetectionRow.source_name.in_(candidates))
                .group_by(DetectionRow.source_name)
                .having(func.count(DetectionRow.id) >= min_detections)
            ).all())
            if not counts:
                return None

            notes = {
                r.source_name: r
                for r in s.scalars(
                    select(SiteNoteRow).where(SiteNoteRow.source_name.in_(counts.keys()))
                )
            }

        # 1. Sites with enough detections but no AI note yet — biggest first.
        missing = sorted(
            (n for n in counts if notes.get(n) is None or notes[n].generated_at is None),
            key=lambda n: counts[n],
            reverse=True,
        )
        if missing:
            return missing[0]

        # 2. Sites that have roughly doubled since last gen.
        grown = []
        for name, row in notes.items():
            at_gen = row.detection_count_at_gen or 0
            if at_gen and counts[name] >= at_gen * regen_count_factor:
                grown.append((name, counts[name] - at_gen))
        if grown:
            grown.sort(key=lambda kv: kv[1], reverse=True)
            return grown[0][0]

        # 3. Aged out — oldest first.
        # SQLite returns naive datetimes; we always WRITE UTC, so treat as such
        # before comparing to the tz-aware ``cutoff``. Without this the worker
        # tick blows up with "can't compare offset-naive and offset-aware".
        def _as_utc(dt):
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

        aged = sorted(
            (n for n, r in notes.items() if r.generated_at and _as_utc(r.generated_at) < cutoff),
            key=lambda n: notes[n].generated_at,
        )
        if aged:
            return aged[0]
        return None

    def set_generated_site_note(
        self,
        source_name: str,
        *,
        note: str,
        generated_by: str,
        evidence_signature: str,
        detection_count_at_gen: int,
    ) -> None:
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(SiteNoteRow, source_name)
            if row is None:
                row = SiteNoteRow(source_name=source_name)
                s.add(row)
            row.note = note
            row.updated_at = now
            row.generated_at = now
            row.generated_by = generated_by
            row.evidence_signature = evidence_signature
            row.detection_count_at_gen = detection_count_at_gen

    def get_site_note(self, source_name: str) -> SiteNoteRow | None:
        with self._Session() as s:
            return s.get(SiteNoteRow, source_name)

    def list_site_notes(self) -> list[SiteNoteRow]:
        with self._Session() as s:
            return list(s.scalars(select(SiteNoteRow)))

    # --- Per-(species, site) notes: how this species sounds at this site ---

    def get_species_site_note(
        self, scientific_name: str, source_name: str
    ) -> SpeciesSiteNoteRow | None:
        with self._Session() as s:
            return s.get(SpeciesSiteNoteRow, (scientific_name, source_name))

    def set_generated_species_site_note(
        self,
        scientific_name: str,
        source_name: str,
        *,
        common_name: str,
        note: str,
        generated_by: str,
        evidence_signature: str,
        detection_count_at_gen: int,
    ) -> None:
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(SpeciesSiteNoteRow, (scientific_name, source_name))
            if row is None:
                row = SpeciesSiteNoteRow(
                    scientific_name=scientific_name, source_name=source_name
                )
                s.add(row)
            row.common_name = common_name
            row.note = note
            row.updated_at = now
            row.generated_at = now
            row.generated_by = generated_by
            row.evidence_signature = evidence_signature
            row.detection_count_at_gen = detection_count_at_gen

    def pick_stale_species_site_for_note(
        self,
        *,
        candidate_sources: Iterable[str],
        max_age_days: int,
        min_detections: int,
        regen_count_factor: float = 2.0,
    ) -> tuple[str, str] | None:
        """Return the (scientific_name, source_name) pair most overdue for a
        per-pair note, or None. Same priority order as the other pickers:
        missing → grown → aged. All comparisons against ``cutoff`` happen in
        SQL so naive-vs-aware datetime issues can't reach Python."""
        candidates = list(candidate_sources)
        if not candidates:
            return None

        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        with self._Session() as s:
            counts_subq = (
                select(
                    DetectionRow.scientific_name.label("sci"),
                    DetectionRow.source_name.label("src"),
                    func.count(DetectionRow.id).label("cnt"),
                )
                .where(DetectionRow.source_name.in_(candidates))
                .group_by(DetectionRow.scientific_name, DetectionRow.source_name)
                .having(func.count(DetectionRow.id) >= min_detections)
                .subquery()
            )

            # 1. Pairs with enough detections but no AI note yet — biggest first.
            missing = s.execute(
                select(counts_subq.c.sci, counts_subq.c.src)
                .outerjoin(
                    SpeciesSiteNoteRow,
                    (SpeciesSiteNoteRow.scientific_name == counts_subq.c.sci)
                    & (SpeciesSiteNoteRow.source_name == counts_subq.c.src),
                )
                .where(SpeciesSiteNoteRow.scientific_name.is_(None))
                .order_by(counts_subq.c.cnt.desc())
                .limit(1)
            ).first()
            if missing:
                return (missing[0], missing[1])

            # 2. Roughly doubled in count since last generation.
            grown = s.execute(
                select(counts_subq.c.sci, counts_subq.c.src)
                .join(
                    SpeciesSiteNoteRow,
                    (SpeciesSiteNoteRow.scientific_name == counts_subq.c.sci)
                    & (SpeciesSiteNoteRow.source_name == counts_subq.c.src),
                )
                .where(SpeciesSiteNoteRow.detection_count_at_gen.is_not(None))
                .where(
                    counts_subq.c.cnt
                    >= SpeciesSiteNoteRow.detection_count_at_gen * regen_count_factor
                )
                .order_by(
                    (counts_subq.c.cnt - SpeciesSiteNoteRow.detection_count_at_gen).desc()
                )
                .limit(1)
            ).first()
            if grown:
                return (grown[0], grown[1])

            # 3. Aged out.
            aged = s.execute(
                select(counts_subq.c.sci, counts_subq.c.src)
                .join(
                    SpeciesSiteNoteRow,
                    (SpeciesSiteNoteRow.scientific_name == counts_subq.c.sci)
                    & (SpeciesSiteNoteRow.source_name == counts_subq.c.src),
                )
                .where(SpeciesSiteNoteRow.generated_at.is_not(None))
                .where(SpeciesSiteNoteRow.generated_at < cutoff)
                .order_by(SpeciesSiteNoteRow.generated_at.asc())
                .limit(1)
            ).first()
            return (aged[0], aged[1]) if aged else None

    def gather_species_site_evidence(
        self, scientific_name: str, source_name: str
    ) -> dict | None:
        """Build the evidence dossier for a (species, site) pair: pair stats
        plus the species' full per-site breakdown for contrast, plus a
        recently-newly-heard signal. Returns None when nothing to summarize."""
        with self._Session() as s:
            common = s.scalar(
                select(DetectionRow.common_name)
                .where(DetectionRow.scientific_name == scientific_name)
                .where(DetectionRow.source_name == source_name)
                .order_by(DetectionRow.started_at.desc())
                .limit(1)
            )
            if common is None:
                return None

            count, mean_conf, max_conf, first_seen, last_seen = s.execute(
                select(
                    func.count(DetectionRow.id),
                    func.avg(DetectionRow.confidence),
                    func.max(DetectionRow.confidence),
                    func.min(DetectionRow.started_at),
                    func.max(DetectionRow.started_at),
                )
                .where(DetectionRow.scientific_name == scientific_name)
                .where(DetectionRow.source_name == source_name)
            ).one()

            # Per-hour pattern AT THIS SITE (UTC bucket; the prompt converts).
            per_hour = s.execute(
                select(
                    func.strftime("%H", DetectionRow.started_at),
                    func.count(DetectionRow.id),
                )
                .where(DetectionRow.scientific_name == scientific_name)
                .where(DetectionRow.source_name == source_name)
                .group_by(func.strftime("%H", DetectionRow.started_at))
            ).all()

            # Per-site totals for this species (full network — for contrast).
            per_source = s.execute(
                select(DetectionRow.source_name, func.count(DetectionRow.id))
                .where(DetectionRow.scientific_name == scientific_name)
                .group_by(DetectionRow.source_name)
            ).all()

            label_counts = dict(s.execute(
                select(DetectionRow.label, func.count(DetectionRow.id))
                .where(DetectionRow.scientific_name == scientific_name)
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.label.is_not(None))
                .group_by(DetectionRow.label)
            ).all())

            # Newly-heard signal: did this site only start hearing it in the
            # last 7 days? (No detections at this source in the prior 7 days.)
            week_ago = datetime.now(UTC) - timedelta(days=7)
            prior = s.scalar(
                select(func.count(DetectionRow.id))
                .where(DetectionRow.scientific_name == scientific_name)
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at < week_ago)
            )

        hourly = [0] * 24
        for hr_str, c in per_hour:
            if hr_str is not None:
                hourly[int(hr_str)] = int(c)

        total_network = sum(int(c) for _, c in per_source)
        return {
            "scientific_name": scientific_name,
            "common_name": common,
            "source_name": source_name,
            "detection_count": int(count or 0),
            "share_of_network": (
                round(int(count or 0) / total_network, 3) if total_network else 0
            ),
            "mean_confidence": round(float(mean_conf or 0), 3),
            "max_confidence": round(float(max_conf or 0), 3),
            "first_seen_utc": first_seen.isoformat() if first_seen else None,
            "last_seen_utc": last_seen.isoformat() if last_seen else None,
            "hourly_utc": hourly,
            "per_source_network": [
                {"source": n, "count": int(c)} for n, c in per_source
            ],
            "labels_at_site": {k: int(v) for k, v in label_counts.items()},
            "newly_heard_at_site": int(prior or 0) == 0,
        }

    # --- Notable-day anomaly detection (volume spike / nocturnal burst / new
    # species wave). Detection is cheap SQL — group-bys against
    # ``detections``. Interpretation lives in the notes worker as one
    # cached Claude call per fired anomaly. ---

    def detect_anomalies_for(
        self, source_name: str, date_utc: date, *,
        tz_utc_offset_hours: int = 2,
        volume_spike_factor: float = 3.0,
        volume_spike_floor: int = 30,
        # Volume-spike requires the baseline ALSO have signal; otherwise
        # every detection at a brand-new site looks like a spike. 10
        # detections/day median is the smallest baseline worth comparing to.
        volume_spike_baseline_floor: float = 10.0,
        nocturnal_burst_ratio: float = 2.0,
        nocturnal_burst_floor: int = 30,
        new_species_floor: int = 3,
        # New-species-wave needs a substantial prior corpus to be meaningful;
        # without it, every species at a new site is "new". 15 species heard
        # in the prior month is a reasonable maturity check.
        new_species_prior_floor: int = 15,
        baseline_window_days: int = 7,
        novelty_window_days: int = 30,
    ) -> list[dict]:
        """Run every detector against (source, date) and return a list of
        triggered anomalies as plain dicts.

        Each detector is a pure SQL aggregation against ``detections``.
        Returning dicts (not rows) keeps this side-effect-free; the caller
        decides whether to ``record_anomaly`` them.

        Detectors:
          * volume_spike  — day's count ≥ ``volume_spike_factor`` × median of
            the prior ``baseline_window_days`` AND ≥ ``volume_spike_floor``.
          * nocturnal_burst — night (local 18:00–06:00) detections ≥
            ``nocturnal_burst_ratio`` × daytime AND ≥ ``nocturnal_burst_floor``.
            Local-time window approximated from ``tz_utc_offset_hours`` —
            UTC range that maps to local night.
          * new_species_wave — ≥ ``new_species_floor`` species appear on
            this date that haven't been heard at this source in the prior
            ``novelty_window_days`` days. Catches stop-over migration pulses.
        """
        from sqlalchemy import func

        with self._Session() as s:
            day_start = datetime.combine(date_utc, time(0, 0), tzinfo=UTC)
            day_end = day_start + timedelta(days=1)

            # All-day count + species set + top-species snapshot.
            total = s.execute(
                select(func.count(DetectionRow.id))
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at >= day_start)
                .where(DetectionRow.started_at < day_end)
            ).scalar() or 0
            if total == 0:
                return []

            today_species = set(s.scalars(
                select(DetectionRow.scientific_name)
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at >= day_start)
                .where(DetectionRow.started_at < day_end)
                .group_by(DetectionRow.scientific_name)
            ).all())

            top_species = list(s.execute(
                select(
                    DetectionRow.common_name,
                    DetectionRow.scientific_name,
                    func.count(DetectionRow.id).label("n"),
                    func.max(DetectionRow.confidence).label("max_conf"),
                )
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at >= day_start)
                .where(DetectionRow.started_at < day_end)
                .group_by(DetectionRow.scientific_name, DetectionRow.common_name)
                .order_by(func.count(DetectionRow.id).desc())
                .limit(10)
            ).all())

            anomalies: list[dict] = []

            # ---- volume_spike ----
            baseline_start = day_start - timedelta(days=baseline_window_days)
            per_day_counts = [
                r[0] for r in s.execute(
                    select(func.count(DetectionRow.id))
                    .where(DetectionRow.source_name == source_name)
                    .where(DetectionRow.started_at >= baseline_start)
                    .where(DetectionRow.started_at < day_start)
                    .group_by(func.date(DetectionRow.started_at))
                ).all()
            ]
            # Pad with zeros for days that had no detections.
            per_day_counts += [0] * max(0, baseline_window_days - len(per_day_counts))
            per_day_counts.sort()
            mid = len(per_day_counts) // 2
            baseline = (
                per_day_counts[mid] if len(per_day_counts) % 2
                else (per_day_counts[mid - 1] + per_day_counts[mid]) / 2
            ) if per_day_counts else 0.0
            if (total >= volume_spike_floor
                and baseline >= volume_spike_baseline_floor
                and total >= baseline * volume_spike_factor):
                anomalies.append({
                    "kind": "volume_spike",
                    "detection_count": total,
                    "baseline_count": float(baseline),
                    "magnitude": total / max(1.0, baseline),
                    "evidence": {
                        "baseline_days": per_day_counts,
                        "top_species": [
                            {
                                "common": r.common_name,
                                "scientific": r.scientific_name,
                                "n": int(r.n),
                                "max_conf": float(r.max_conf or 0),
                            }
                            for r in top_species
                        ],
                    },
                })

            # ---- nocturnal_burst ----
            # Local night = 18:00–06:00 local. Convert to UTC hours using
            # the site's offset. SQLite holds wall-clock UTC strings, so
            # we can pick the matching utc hours directly.
            #
            # E.g. for UTC+3 (EAT): night = 15:00–03:00 UTC.
            #      for UTC+2 (SAST/CAT): night = 16:00–04:00 UTC.
            night_hours_utc = set()
            for local_h in list(range(18, 24)) + list(range(0, 6)):
                utc_h = (local_h - tz_utc_offset_hours) % 24
                night_hours_utc.add(utc_h)
            # Hourly histogram.
            hourly = dict(s.execute(
                select(
                    func.cast(
                        func.strftime("%H", DetectionRow.started_at),
                        Integer,
                    ),
                    func.count(DetectionRow.id),
                )
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at >= day_start)
                .where(DetectionRow.started_at < day_end)
                .group_by(func.strftime("%H", DetectionRow.started_at))
            ).all())
            night_n = sum(n for h, n in hourly.items() if h in night_hours_utc)
            day_n = sum(n for h, n in hourly.items() if h not in night_hours_utc)
            # Require ≥5 daytime detections so we don't false-fire when a
            # site was only online at night (e.g. brand-new sources added
            # late in the day get all-nocturnal coverage by accident).
            if (night_n >= nocturnal_burst_floor
                and day_n >= 5
                and night_n >= day_n * nocturnal_burst_ratio):
                anomalies.append({
                    "kind": "nocturnal_burst",
                    "detection_count": night_n,
                    "baseline_count": float(day_n),
                    "magnitude": night_n / max(1.0, day_n),
                    "evidence": {
                        "night_hours_utc": sorted(night_hours_utc),
                        "hourly_utc": {int(h): int(n) for h, n in hourly.items()},
                        "top_species": [
                            {
                                "common": r.common_name,
                                "scientific": r.scientific_name,
                                "n": int(r.n),
                                "max_conf": float(r.max_conf or 0),
                            }
                            for r in top_species
                        ],
                    },
                })

            # ---- new_species_wave ----
            novelty_start = day_start - timedelta(days=novelty_window_days)
            prior_species = set(s.scalars(
                select(DetectionRow.scientific_name)
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at >= novelty_start)
                .where(DetectionRow.started_at < day_start)
                .group_by(DetectionRow.scientific_name)
            ).all())
            new_species = today_species - prior_species
            if (len(new_species) >= new_species_floor
                and len(prior_species) >= new_species_prior_floor):
                # Pull commons + counts for the *new* species so the LLM can
                # interpret without re-querying.
                new_rows = list(s.execute(
                    select(
                        DetectionRow.common_name,
                        DetectionRow.scientific_name,
                        func.count(DetectionRow.id).label("n"),
                        func.max(DetectionRow.confidence).label("max_conf"),
                    )
                    .where(DetectionRow.source_name == source_name)
                    .where(DetectionRow.started_at >= day_start)
                    .where(DetectionRow.started_at < day_end)
                    .where(DetectionRow.scientific_name.in_(new_species))
                    .group_by(
                        DetectionRow.scientific_name, DetectionRow.common_name,
                    )
                    .order_by(func.count(DetectionRow.id).desc())
                ).all())
                anomalies.append({
                    "kind": "new_species_wave",
                    "detection_count": sum(int(r.n) for r in new_rows),
                    "baseline_count": float(novelty_window_days),  # context
                    "magnitude": float(len(new_species)),
                    "evidence": {
                        "novelty_window_days": novelty_window_days,
                        "new_species": [
                            {
                                "common": r.common_name,
                                "scientific": r.scientific_name,
                                "n": int(r.n),
                                "max_conf": float(r.max_conf or 0),
                            }
                            for r in new_rows
                        ],
                    },
                })

        return anomalies

    def record_anomaly(
        self, source_name: str, date_utc: date, anomaly: dict,
    ) -> bool:
        """Insert (or skip if already present). Returns True iff a new row
        was created.

        If ``anomaly['interpretation']`` is provided (the deterministic
        kinds — first_live_day, down_day — fill it in), the row is written
        ready-to-display and the notes worker won't burn an LLM call on it.
        Otherwise the row goes in with interpretation=NULL and the worker
        picks it up on the next anomaly_tick."""
        import json as _json
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            existing = s.get(
                AnomalyEventRow,
                (source_name, date_utc, anomaly["kind"]),
            )
            if existing is not None:
                return False
            interp = anomaly.get("interpretation")
            row = AnomalyEventRow(
                source_name=source_name,
                date_utc=date_utc,
                kind=anomaly["kind"],
                detection_count=int(anomaly.get("detection_count") or 0),
                baseline_count=anomaly.get("baseline_count"),
                magnitude=float(anomaly["magnitude"]),
                evidence_json=_json.dumps(anomaly["evidence"]),
                interpretation=interp,
                generated_by="deterministic" if interp else None,
                generated_at=now if interp else None,
                created_at=now,
            )
            s.add(row)
        return True

    # ---- Operational/factual anomaly detectors (deterministic, no LLM) ----

    def detect_first_live(self, source_name: str) -> dict | None:
        """The first calendar day this source produced detections. Returns
        an anomaly dict with date pre-attached and a deterministic
        interpretation already filled in — emit once per source, ever."""
        with self._Session() as s:
            first_at = s.execute(
                select(func.min(DetectionRow.started_at))
                .where(DetectionRow.source_name == source_name)
            ).scalar()
            if first_at is None:
                return None
            if first_at.tzinfo is None:
                first_at = first_at.replace(tzinfo=UTC)
            first_date = first_at.date()
            day_start = datetime.combine(first_date, time(0, 0), tzinfo=UTC)
            day_end = day_start + timedelta(days=1)
            n = s.execute(
                select(func.count(DetectionRow.id))
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at >= day_start)
                .where(DetectionRow.started_at < day_end)
            ).scalar() or 0
            # Total distinct species on day one.
            n_species = s.execute(
                select(func.count(func.distinct(DetectionRow.scientific_name)))
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at >= day_start)
                .where(DetectionRow.started_at < day_end)
            ).scalar() or 0
            # Top 5 by count — for the evidence dossier + naming the
            # "first calls included …" phrase.
            top_species = list(s.execute(
                select(
                    DetectionRow.common_name,
                    DetectionRow.scientific_name,
                    func.count().label("n"),
                    func.max(DetectionRow.confidence).label("max_conf"),
                )
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.started_at >= day_start)
                .where(DetectionRow.started_at < day_end)
                .group_by(
                    DetectionRow.scientific_name, DetectionRow.common_name,
                )
                .order_by(func.count().desc())
                .limit(5)
            ).all())

        top_names = [r.common_name for r in top_species[:3]]
        if len(top_names) >= 3:
            top_phrase = (
                f"{top_names[0]}, {top_names[1]}, and {top_names[2]}"
            )
        elif top_names:
            top_phrase = ", ".join(top_names)
        else:
            top_phrase = "(no species above the confidence floor on this day)"
        interp = (
            f"{source_name} came online on this day. "
            f"{n} detection{'s' if n != 1 else ''} were captured across "
            f"{n_species} species — first calls included {top_phrase}."
        )
        return {
            "kind": "first_live_day",
            "date": first_date,
            "detection_count": int(n),
            "baseline_count": None,
            "magnitude": float(n_species),
            "evidence": {
                "first_detection_utc": first_at.isoformat(),
                "top_species": [
                    {
                        "common": r.common_name,
                        "scientific": r.scientific_name,
                        "n": int(r.n),
                        "max_conf": float(r.max_conf or 0),
                    }
                    for r in top_species
                ],
            },
            "interpretation": interp,
        }

    def detect_down_day(
        self, source_name: str, date_utc: date,
        *, downtime_floor_s: int = 3600,
    ) -> dict | None:
        """Sum ``worker_downtime`` intervals that overlap ``date_utc`` for
        this source. Emit if total ≥ ``downtime_floor_s`` (default 1 h —
        the routine 10-20 s stream-EOF reconnects don't qualify).

        Returns an anomaly dict with deterministic interpretation already
        written (no LLM needed for an operational fact)."""
        day_start = datetime.combine(date_utc, time(0, 0), tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        with self._Session() as s:
            intervals = list(s.execute(
                select(
                    WorkerDowntimeRow.started_at,
                    WorkerDowntimeRow.ended_at,
                    WorkerDowntimeRow.reason,
                )
                .where(WorkerDowntimeRow.source_name == source_name)
                .where(or_(
                    WorkerDowntimeRow.ended_at.is_(None),
                    WorkerDowntimeRow.ended_at >= day_start,
                ))
                .where(WorkerDowntimeRow.started_at < day_end)
            ).all())

        total_s = 0.0
        longest_s = 0.0
        longest_reason: str | None = None
        outages: list[dict] = []
        now = datetime.now(UTC)
        for start_at, end_at, reason in intervals:
            if start_at.tzinfo is None:
                start_at = start_at.replace(tzinfo=UTC)
            s_eff = max(start_at, day_start)
            if end_at is None:
                e_eff = min(now, day_end)
            else:
                if end_at.tzinfo is None:
                    end_at = end_at.replace(tzinfo=UTC)
                e_eff = min(end_at, day_end)
            if e_eff > s_eff:
                dur = (e_eff - s_eff).total_seconds()
                total_s += dur
                if dur > longest_s:
                    longest_s = dur
                    longest_reason = reason
                outages.append({
                    "started_at": start_at.isoformat(),
                    "duration_s": int(dur),
                    "reason": reason or "",
                })

        if total_s < downtime_floor_s:
            return None

        def _fmt(s: float) -> str:
            if s < 60:
                return f"{int(s)} s"
            if s < 3600:
                return f"{int(s / 60)} min"
            return f"{s / 3600:.1f} h"

        n_out = len(outages)
        interp_parts = [
            f"{source_name} was offline for {_fmt(total_s)} on this day "
            f"across {n_out} outage{'s' if n_out != 1 else ''}."
        ]
        if longest_s > 0:
            tail = f" ({longest_reason})" if longest_reason else ""
            interp_parts.append(
                f"Longest interruption: {_fmt(longest_s)}{tail}."
            )
        interp = " ".join(interp_parts)

        return {
            "kind": "down_day",
            "detection_count": 0,
            "baseline_count": None,
            "magnitude": total_s / 3600.0,  # hours
            "evidence": {
                "total_seconds": int(total_s),
                "n_outages": n_out,
                "longest_seconds": int(longest_s),
                "longest_reason": longest_reason,
                "outages": outages,
            },
            "interpretation": interp,
        }

    def list_uninterpreted_anomalies(self, limit: int = 20) -> list[AnomalyEventRow]:
        """Detected-but-not-yet-explained anomalies. Notes worker picks
        these up oldest-first."""
        with self._Session() as s:
            return list(s.scalars(
                select(AnomalyEventRow)
                .where(AnomalyEventRow.interpretation.is_(None))
                .order_by(AnomalyEventRow.created_at)
                .limit(limit)
            ))

    def set_anomaly_interpretation(
        self, source_name: str, date_utc: date, kind: str,
        *, interpretation: str, generated_by: str,
    ) -> None:
        with self._Session() as s, s.begin():
            row = s.get(AnomalyEventRow, (source_name, date_utc, kind))
            if row is None:
                return
            row.interpretation = interpretation
            row.generated_by = generated_by
            row.generated_at = datetime.now(UTC)

    def list_recent_anomalies(
        self, source_name: str, days: int = 30,
    ) -> list[AnomalyEventRow]:
        """For the /site/<name> Notable Days panel."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date()
        with self._Session() as s:
            return list(s.scalars(
                select(AnomalyEventRow)
                .where(AnomalyEventRow.source_name == source_name)
                .where(AnomalyEventRow.date_utc >= cutoff)
                .order_by(AnomalyEventRow.date_utc.desc(),
                          AnomalyEventRow.kind)
            ))

    # --- Daily soundscape brief (one row per UTC date, generated once) ---

    def gather_daily_evidence(self, date_utc: date) -> dict | None:
        """Aggregate one UTC day across all sites for the daily-brief worker.

        Returns None if there are no detections on that date — keeps the
        worker from generating an empty brief for a day the pipeline was
        down. Includes a "newly heard this week" list (species that appear
        on the date but weren't heard the prior 7 days at that source)
        which is the most interesting story angle for the digest."""
        from sqlalchemy import func

        start = datetime.combine(date_utc, time(0, 0), tzinfo=UTC)
        end = start + timedelta(days=1)
        week_start = start - timedelta(days=7)

        with self._Session() as s:
            base = (
                select(
                    func.count(DetectionRow.id),
                    func.count(func.distinct(DetectionRow.scientific_name)),
                )
                .where(DetectionRow.started_at >= start)
                .where(DetectionRow.started_at < end)
            )
            total, distinct = s.execute(base).one()
            if not total:
                return None

            per_site_count = dict(s.execute(
                select(DetectionRow.source_name, func.count(DetectionRow.id))
                .where(DetectionRow.started_at >= start)
                .where(DetectionRow.started_at < end)
                .group_by(DetectionRow.source_name)
            ).all())

            per_site_top = s.execute(
                select(
                    DetectionRow.source_name,
                    DetectionRow.scientific_name,
                    DetectionRow.common_name,
                    func.count(DetectionRow.id),
                    func.max(DetectionRow.confidence),
                )
                .where(DetectionRow.started_at >= start)
                .where(DetectionRow.started_at < end)
                .group_by(DetectionRow.source_name, DetectionRow.scientific_name)
                .order_by(
                    DetectionRow.source_name,
                    func.count(DetectionRow.id).desc(),
                )
            ).all()

            # Order by count desc so the first row per source = its peak hour.
            peak_rows = s.execute(
                select(
                    DetectionRow.source_name,
                    func.strftime("%H", DetectionRow.started_at),
                    func.count(DetectionRow.id),
                )
                .where(DetectionRow.started_at >= start)
                .where(DetectionRow.started_at < end)
                .group_by(
                    DetectionRow.source_name,
                    func.strftime("%H", DetectionRow.started_at),
                )
                .order_by(func.count(DetectionRow.id).desc())
            ).all()
            per_site_peak: dict[str, int | None] = {}
            for src_n, hr_str, _ in peak_rows:
                if src_n not in per_site_peak and hr_str is not None:
                    per_site_peak[src_n] = int(hr_str)

            standouts = s.execute(
                select(
                    DetectionRow.scientific_name,
                    DetectionRow.common_name,
                    DetectionRow.source_name,
                    DetectionRow.confidence,
                )
                .where(DetectionRow.started_at >= start)
                .where(DetectionRow.started_at < end)
                .where(DetectionRow.confidence >= 0.9)
                .order_by(DetectionRow.confidence.desc())
                .limit(8)
            ).all()

            # Newly heard: species at a source today that we did NOT detect
            # at that source during the prior 7 days.
            today_pairs = set(s.execute(
                select(DetectionRow.source_name, DetectionRow.scientific_name)
                .where(DetectionRow.started_at >= start)
                .where(DetectionRow.started_at < end)
                .distinct()
            ).all())
            prior_pairs = set(s.execute(
                select(DetectionRow.source_name, DetectionRow.scientific_name)
                .where(DetectionRow.started_at >= week_start)
                .where(DetectionRow.started_at < start)
                .distinct()
            ).all())
            new_pairs = today_pairs - prior_pairs

            common_lookup = {}
            if new_pairs:
                sci_names = {sci for _, sci in new_pairs}
                for sci, com in s.execute(
                    select(DetectionRow.scientific_name, DetectionRow.common_name)
                    .where(DetectionRow.scientific_name.in_(sci_names))
                    .group_by(DetectionRow.scientific_name)
                ).all():
                    common_lookup[sci] = com

        # Group top species by site (keep top 3 per site).
        per_site_top_by_src: dict[str, list[dict]] = {}
        for src, sci, com, c, mc in per_site_top:
            bucket = per_site_top_by_src.setdefault(src, [])
            if len(bucket) < 3:
                bucket.append({
                    "scientific_name": sci,
                    "common_name": com,
                    "count": int(c),
                    "max_confidence": round(float(mc), 3),
                })

        return {
            "date_utc": date_utc.isoformat(),
            "total_detections": int(total),
            "distinct_species": int(distinct),
            "per_site": [
                {
                    "source_name": src,
                    "count": int(per_site_count.get(src, 0)),
                    "peak_hour_utc": per_site_peak.get(src),
                    "top_species": per_site_top_by_src.get(src, []),
                }
                for src in sorted(per_site_count.keys())
            ],
            "high_confidence_standouts": [
                {
                    "scientific_name": sci,
                    "common_name": com,
                    "source_name": src,
                    "confidence": round(float(conf), 3),
                }
                for sci, com, src, conf in standouts
            ],
            "newly_heard_this_week": [
                {
                    "source_name": src,
                    "scientific_name": sci,
                    "common_name": common_lookup.get(sci, ""),
                }
                for src, sci in sorted(new_pairs)
            ],
        }

    def missing_daily_briefs(self, lookback_days: int = 3) -> list[date]:
        """Return UTC dates within the lookback for which a brief is missing
        AND data exists. Excludes today (in-progress). Oldest first so the
        worker fills gaps in order."""
        from sqlalchemy import func

        today = datetime.now(UTC).date()
        earliest = today - timedelta(days=lookback_days)
        with self._Session() as s:
            existing = {
                r.date_utc
                for r in s.scalars(
                    select(DailyBriefRow).where(DailyBriefRow.date_utc >= earliest)
                )
            }
            days_with_data = set(s.execute(
                select(func.distinct(func.date(DetectionRow.started_at)))
                .where(
                    DetectionRow.started_at
                    >= datetime.combine(earliest, time(0, 0), tzinfo=UTC)
                )
                .where(
                    DetectionRow.started_at
                    < datetime.combine(today, time(0, 0), tzinfo=UTC)
                )
            ).scalars())

        # SQLite returns date strings here; normalize.
        normalized = set()
        for d in days_with_data:
            if isinstance(d, str):
                normalized.add(date.fromisoformat(d))
            else:
                normalized.add(d)
        return sorted(normalized - existing)

    def set_daily_brief(
        self,
        date_utc: date,
        *,
        brief_text: str,
        generated_by: str,
        total_detections: int,
        distinct_species: int,
        evidence_signature: str,
    ) -> None:
        """Insert (or replace) the brief for one UTC date. Idempotent so a
        manual re-trigger from the shell can overwrite a bad first draft."""
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(DailyBriefRow, date_utc)
            if row is None:
                row = DailyBriefRow(date_utc=date_utc)
                s.add(row)
            row.brief_text = brief_text
            row.generated_at = now
            row.generated_by = generated_by
            row.total_detections = total_detections
            row.distinct_species = distinct_species
            row.evidence_signature = evidence_signature

    def list_daily_briefs(self, limit: int = 60) -> list[DailyBriefRow]:
        with self._Session() as s:
            return list(s.scalars(
                select(DailyBriefRow).order_by(DailyBriefRow.date_utc.desc()).limit(limit)
            ))

    def get_latest_daily_brief(self) -> DailyBriefRow | None:
        with self._Session() as s:
            return s.scalar(
                select(DailyBriefRow).order_by(DailyBriefRow.date_utc.desc()).limit(1)
            )

    # --- Weather archive (filled by the background weather worker) ---

    @staticmethod
    def _coord_key(lat: float, lon: float) -> tuple[int, int]:
        return int(round(lat * 1000)), int(round(lon * 1000))

    def upsert_weather_observations(
        self,
        lat: float,
        lon: float,
        rows: Iterable[dict],
    ) -> tuple[int, int]:
        """Bulk-upsert a batch of hourly observations for one coordinate.

        ``rows`` is an iterable of dicts shaped like::

            {
                "observed_at_utc": datetime (tz-aware),
                "temp_c": float | None,
                "humidity_pct": float | None,
                "precipitation_mm": float | None,
                "wind_kph": float | None,
                "cloud_cover_pct": float | None,
                "wmo_code": int | None,
            }

        Returns ``(inserted, revised)`` so the worker can log a meaningful
        tick summary. A revised row is one whose values changed since the
        last fetch (Open-Meteo refines very recent hours as data arrives).
        """
        lat_e3, lon_e3 = self._coord_key(lat, lon)
        now = datetime.now(UTC)
        inserted = revised = 0
        with self._Session() as s, s.begin():
            for r in rows:
                obs_at = r["observed_at_utc"]
                if obs_at.tzinfo is None:
                    obs_at = obs_at.replace(tzinfo=UTC)
                pk = (lat_e3, lon_e3, obs_at)
                existing = s.get(WeatherObservationRow, pk)
                if existing is None:
                    s.add(WeatherObservationRow(
                        lat_e3=lat_e3,
                        lon_e3=lon_e3,
                        observed_at_utc=obs_at,
                        temp_c=r.get("temp_c"),
                        humidity_pct=r.get("humidity_pct"),
                        precipitation_mm=r.get("precipitation_mm"),
                        wind_kph=r.get("wind_kph"),
                        cloud_cover_pct=r.get("cloud_cover_pct"),
                        wmo_code=r.get("wmo_code"),
                        fetched_at=now,
                    ))
                    inserted += 1
                else:
                    changed = (
                        existing.temp_c != r.get("temp_c")
                        or existing.humidity_pct != r.get("humidity_pct")
                        or existing.precipitation_mm != r.get("precipitation_mm")
                        or existing.wind_kph != r.get("wind_kph")
                        or existing.cloud_cover_pct != r.get("cloud_cover_pct")
                        or existing.wmo_code != r.get("wmo_code")
                    )
                    if changed:
                        existing.temp_c = r.get("temp_c")
                        existing.humidity_pct = r.get("humidity_pct")
                        existing.precipitation_mm = r.get("precipitation_mm")
                        existing.wind_kph = r.get("wind_kph")
                        existing.cloud_cover_pct = r.get("cloud_cover_pct")
                        existing.wmo_code = r.get("wmo_code")
                        existing.fetched_at = now
                        revised += 1
        return inserted, revised

    def latest_weather_observation_at(
        self, lat: float, lon: float
    ) -> WeatherObservationRow | None:
        """Most recently observed hour for this coord — used to decide whether
        a backfill is still needed or a steady-state tick suffices."""
        lat_e3, lon_e3 = self._coord_key(lat, lon)
        with self._Session() as s:
            return s.scalar(
                select(WeatherObservationRow)
                .where(WeatherObservationRow.lat_e3 == lat_e3)
                .where(WeatherObservationRow.lon_e3 == lon_e3)
                .order_by(WeatherObservationRow.observed_at_utc.desc())
                .limit(1)
            )

    def weather_hour_summary(
        self,
        lat: float,
        lon: float,
        local_hour: int,
        since: datetime,
        until: datetime,
        tz_name: str,
    ) -> dict | None:
        """Aggregate stored weather for ``local_hour`` (0-23) in ``tz_name``
        across [since, until]. Returns the same shape as
        ``africam.weather.weather_at_hour`` so the existing templates render
        unchanged: ``{n_samples, temp_mean, wmo_code, icon, label}``.
        Returns None when no rows match — caller should hide the block.
        """
        from africam.weather import wmo_summary

        lat_e3, lon_e3 = self._coord_key(lat, lon)
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = UTC  # type: ignore[assignment]

        with self._Session() as s:
            rows = list(s.scalars(
                select(WeatherObservationRow)
                .where(WeatherObservationRow.lat_e3 == lat_e3)
                .where(WeatherObservationRow.lon_e3 == lon_e3)
                .where(WeatherObservationRow.observed_at_utc >= since)
                .where(WeatherObservationRow.observed_at_utc < until)
            ))

        temps: list[float] = []
        codes: list[int] = []
        n = 0
        for r in rows:
            ts = r.observed_at_utc
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts.astimezone(tz).hour != local_hour:
                continue
            n += 1
            if r.temp_c is not None:
                temps.append(r.temp_c)
            if r.wmo_code is not None:
                codes.append(r.wmo_code)
        if n == 0:
            return None
        modal: int | None = None
        if codes:
            counts: dict[int, int] = {}
            for c in codes:
                counts[c] = counts.get(c, 0) + 1
            modal = max(counts, key=counts.get)
        icon, label = wmo_summary(modal)
        return {
            "n_samples": n,
            "temp_mean": (sum(temps) / len(temps)) if temps else None,
            "wmo_code": modal,
            "icon": icon,
            "label": label,
        }
