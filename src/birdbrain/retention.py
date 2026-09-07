"""Clip retention for central: what to delete, what to keep, and why.

The database keeps every detection row forever — a row is ~200 bytes. The
audio behind it is ~50 KB, and at 50k detections a day across two Pis that is
2.5 GB a day, so clips get a window. Three rules decide what survives it:

1. **Age.** A clip older than ``days`` goes. A species with its own override
   (``SpeciesNoteRow.clip_retention_days``) uses that instead — set it to a day
   or two for loud, unmistakable birds (Egyptian Goose, Hadada Ibis, Grey
   Go-away-bird) whose thousands of clips a month nobody will ever audition.
2. **Anything a person touched stays.** A label, a suggested species or a
   sound rating means someone auditioned it, and that judgement is worth more
   than the disk.
3. **A reference set stays.** For every species at every source, the newest
   clip in each confidence band (low, mid, high) is kept regardless of age. It
   is the smallest set that still lets you hear what a 0.4 and a 0.9 of that
   species sound like *here* — a few thousand files, a few hundred MB.

A clip file is shared by every species BirdNET heard in the same window, so a
file is only deleted when *no* row that references it is protected. Rows whose
file is kept for someone else's sake keep their ``clip_path``; rows whose file
goes have it NULLed, exactly as ``birdbrain prune`` always did.

Work is done a day at a time, oldest first. The first run on a store with
months of clips walks a million rows; per-day batches keep memory flat and let
a killed run resume where it stopped.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import case, func, or_, select

from birdbrain.logging import get_logger
from birdbrain.storage import Database, DetectionRow

log = get_logger(__name__)

# Confidence bands for the reference set: low < BAND_MID <= mid < BAND_HIGH <= high.
BAND_MID = 0.5
BAND_HIGH = 0.8

# Rows NULLed per UPDATE. SQLite's default variable cap is 32766 on modern
# builds; stay well under it.
_UPDATE_CHUNK = 2000


@dataclass(frozen=True)
class RetentionPolicy:
    days: int = 30
    #: scientific_name -> days, overriding ``days`` for that species
    species_days: dict[str, int] = field(default_factory=dict)
    keep_audited: bool = True
    keep_reference_set: bool = True

    def cutoff_for(self, scientific_name: str, now: datetime) -> datetime:
        return now - timedelta(days=self.species_days.get(scientific_name, self.days))


@dataclass
class PruneStats:
    days_scanned: int = 0
    rows_seen: int = 0
    rows_nulled: int = 0
    rows_kept_shared_file: int = 0
    files_deleted: int = 0
    files_missing: int = 0
    bytes_freed: int = 0
    siblings_deleted: int = 0
    protected_files: int = 0
    by_source: dict[str, int] = field(default_factory=dict)

    @property
    def mb_freed(self) -> float:
        return self.bytes_freed / 1024 / 1024


def policy_from_db(db: Database, days: int) -> RetentionPolicy:
    """The global window plus every per-species override stored on central."""
    return RetentionPolicy(days=days, species_days=db.species_clip_retention_map())


def protected_clip_paths(db: Database, policy: RetentionPolicy) -> set[str]:
    """Every clip file that must survive this sweep, whatever its age.

    Two sources: rows a person audited, and the reference set — the newest
    row per (species, source, band) among all rows that still have a clip.
    ``max(id)`` stands in for newest: ids ascend with insertion on both the
    local pipeline and ingest, and it needs no subquery.
    """
    protected: set[str] = set()
    with db.session() as s:
        if policy.keep_audited:
            rows = s.execute(
                select(DetectionRow.clip_path)
                .where(DetectionRow.clip_path.is_not(None))
                .where(or_(
                    DetectionRow.label.is_not(None),
                    DetectionRow.suggested_species.is_not(None),
                    DetectionRow.sound_rating.is_not(None),
                ))
                .distinct()
            )
            protected.update(p for (p,) in rows if p)
        if policy.keep_reference_set:
            band = case(
                (DetectionRow.confidence < BAND_MID, 0),
                (DetectionRow.confidence < BAND_HIGH, 1),
                else_=2,
            )
            newest = (
                select(func.max(DetectionRow.id))
                .where(DetectionRow.clip_path.is_not(None))
                .group_by(DetectionRow.scientific_name, DetectionRow.source_name, band)
            )
            rows = s.execute(
                select(DetectionRow.clip_path)
                .where(DetectionRow.id.in_(newest))
                .distinct()
            )
            protected.update(p for (p,) in rows if p)
    return protected


def _day_bounds(db: Database, policy: RetentionPolicy, now: datetime) -> tuple[date, date] | None:
    """(oldest day with a clip, last day any window could reach) or None."""
    with db.session() as s:
        oldest = s.scalar(
            select(func.min(DetectionRow.started_at)).where(DetectionRow.clip_path.is_not(None))
        )
    if oldest is None:
        return None
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    shortest = min([policy.days, *policy.species_days.values()])
    latest = now - timedelta(days=shortest)
    if latest.date() < oldest.date():
        return None
    return oldest.date(), latest.date()


def _expired_rows_on(
    db: Database, policy: RetentionPolicy, day: date, now: datetime
) -> list[tuple[int, str, str]]:
    """(id, clip_path, source_name) for rows started on ``day`` that have
    outlived their window and were never audited."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    global_cutoff = now - timedelta(days=policy.days)
    # Per-species windows, grouped by length so each distinct cutoff is one
    # predicate rather than one per species.
    by_days: dict[int, list[str]] = {}
    for sci, d in policy.species_days.items():
        by_days.setdefault(d, []).append(sci)
    expired = [DetectionRow.started_at < global_cutoff]
    for d, species in by_days.items():
        expired.append(
            (DetectionRow.scientific_name.in_(species))
            & (DetectionRow.started_at < now - timedelta(days=d))
        )
    stmt = (
        select(DetectionRow.id, DetectionRow.clip_path, DetectionRow.source_name)
        .where(DetectionRow.clip_path.is_not(None))
        .where(DetectionRow.started_at >= start)
        .where(DetectionRow.started_at < end)
        .where(or_(*expired))
    )
    if policy.keep_audited:
        stmt = (
            stmt.where(DetectionRow.label.is_(None))
            .where(DetectionRow.suggested_species.is_(None))
            .where(DetectionRow.sound_rating.is_(None))
        )
    with db.session() as s:
        return [(i, p, src) for i, p, src in s.execute(stmt)]


def _remove_clip(path: Path, stats: PruneStats, dry_run: bool) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        stats.files_missing += 1
        size = 0
    # Cached siblings: the on-demand MP3 and every spectrogram palette/size.
    # Stems are timestamps, unique within a day directory.
    siblings = [p for p in path.parent.glob(path.stem + ".*") if p != path]
    if not dry_run:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        for sib in siblings:
            try:
                stats.bytes_freed += sib.stat().st_size
                sib.unlink()
                stats.siblings_deleted += 1
            except OSError:
                continue
    else:
        for sib in siblings:
            try:
                stats.bytes_freed += sib.stat().st_size
                stats.siblings_deleted += 1
            except OSError:
                continue
    if size:
        stats.files_deleted += 1
        stats.bytes_freed += size


def prune_clips(
    db: Database,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    on_day: Callable[[date, PruneStats], None] | None = None,
) -> PruneStats:
    """Apply ``policy``: delete expired clip files and NULL their rows.

    ``dry_run`` walks and counts everything without touching disk or rows.
    ``on_day`` is called after each day's batch, for progress output.
    """
    now = now or datetime.now(UTC)
    stats = PruneStats()
    bounds = _day_bounds(db, policy, now)
    if bounds is None:
        return stats
    protected = protected_clip_paths(db, policy)
    stats.protected_files = len(protected)

    day, last = bounds
    while day <= last:
        rows = _expired_rows_on(db, policy, day, now)
        stats.days_scanned += 1
        stats.rows_seen += len(rows)
        by_file: dict[str, list[int]] = {}
        for det_id, path, source in rows:
            if path in protected:
                stats.rows_kept_shared_file += 1
                continue
            by_file.setdefault(path, []).append(det_id)
            stats.by_source[source] = stats.by_source.get(source, 0) + 1

        to_null: list[int] = []
        for path, ids in by_file.items():
            _remove_clip(Path(path), stats, dry_run)
            to_null.extend(ids)
        if not dry_run:
            for i in range(0, len(to_null), _UPDATE_CHUNK):
                stats.rows_nulled += db.set_clip_path_many(to_null[i:i + _UPDATE_CHUNK], None)
            _rmdir_if_empty(by_file)
        else:
            stats.rows_nulled += len(to_null)
        if on_day is not None:
            on_day(day, stats)
        day += timedelta(days=1)

    log.info(
        "retention.pruned" if not dry_run else "retention.dry_run",
        days=policy.days, overrides=len(policy.species_days),
        files=stats.files_deleted, rows=stats.rows_nulled,
        mb=round(stats.mb_freed, 1), protected_files=stats.protected_files,
    )
    return stats


def _rmdir_if_empty(by_file: dict[str, list[int]]) -> None:
    for d in {Path(p).parent for p in by_file}:
        with contextlib.suppress(OSError):
            d.rmdir()  # fails unless empty — that is the check
