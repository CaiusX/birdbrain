from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from africam.detector.birdnet import Detection
from africam.storage.models import (
    Base,
    DetectionRow,
    RuntimeSourceRow,
    SourceStateRow,
    SpeciesNoteRow,
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
