from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from africam.detector.birdnet import Detection
from africam.storage.models import Base, DetectionRow, RuntimeSourceRow, SourceStateRow


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
