from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from africam.detector.birdnet import Detection
from africam.storage.models import (
    Base,
    DailyBriefRow,
    DetectionRow,
    RuntimeSourceRow,
    SiteNoteRow,
    SourceStateRow,
    SpeciesNoteRow,
    SpeciesSiteNoteRow,
    WorkerHeartbeatRow,
)

# Sentinel for "argument not supplied" — distinct from None, which means
# "explicitly clear this field."
_UNSET: Any = object()


class Database:
    def __init__(self, url: str) -> None:
        # Ensure parent dir exists for sqlite file URLs.
        if url.startswith("sqlite:///"):
            db_path = Path(url.removeprefix("sqlite:///"))
            db_path.parent.mkdir(parents=True, exist_ok=True)

        self.engine = create_engine(url, future=True)
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
            ("species_notes", "media_fetched_at", "TIMESTAMP"),
            ("runtime_sources", "timezone", "TEXT DEFAULT 'UTC'"),
        ]
        with self.engine.begin() as conn:
            for table, col, ddl in added:
                existing = {
                    row[1]
                    for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
                }
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    def session(self) -> Session:
        return self._Session()

    def insert_detections(
        self,
        detections: Iterable[Detection],
        clip_path: str | None = None,
        site: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
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
            )
            for d in detections
        ]
        if not rows:
            return 0
        with self._Session() as s, s.begin():
            s.add_all(rows)
        return len(rows)

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
    ) -> bool:
        """Set/clear the manual audition label on a detection. Returns False if
        the detection doesn't exist.

        ``suggested`` is the rater's free-text guess at the actual species. It
        is only meaningful when ``label`` is ``'bad'`` or ``'unsure'``; for any
        other label this method clears it automatically. Pass the sentinel
        default to leave the existing suggestion unchanged."""
        if label is not None and label not in ("good", "bad", "unsure"):
            raise ValueError(f"invalid label: {label!r}")
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
        return True

    # --- Worker heartbeats (admin liveness view) ---

    def worker_started(self, source_name: str) -> None:
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(WorkerHeartbeatRow, source_name)
            if row is None:
                row = WorkerHeartbeatRow(source_name=source_name)
                s.add(row)
            row.started_at = now
            row.last_heartbeat_at = now
            row.state = "running"
            row.last_error = None

    def worker_heartbeat(self, source_name: str) -> None:
        """Bump last_heartbeat_at. Called every ~15s while the worker is healthy."""
        now = datetime.now(UTC)
        with self._Session() as s, s.begin():
            row = s.get(WorkerHeartbeatRow, source_name)
            if row is None:
                row = WorkerHeartbeatRow(
                    source_name=source_name, started_at=now, last_heartbeat_at=now
                )
                s.add(row)
            else:
                row.last_heartbeat_at = now
                row.state = "running"
                row.last_error = None

    def worker_backoff(self, source_name: str, error: str) -> None:
        with self._Session() as s, s.begin():
            row = s.get(WorkerHeartbeatRow, source_name)
            if row is None:
                row = WorkerHeartbeatRow(
                    source_name=source_name,
                    started_at=datetime.now(UTC),
                    last_heartbeat_at=datetime.now(UTC),
                )
                s.add(row)
            row.state = "backoff"
            row.last_error = (error or "")[:512] or None
            row.last_heartbeat_at = datetime.now(UTC)

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
