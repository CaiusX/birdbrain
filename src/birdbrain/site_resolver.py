"""Combines manual override (DB-backed) with OCR auto-detection (in-memory).

Resolution order on each call:

1. If the SourceState row has an unexpired ``manual_until``, use that site.
2. Else if the OCR watcher has produced a recent match, use that site.
3. Else fall back to the source's static lat/lon from sources.toml,
   with site=None.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from birdbrain.config import SourceConfig
from birdbrain.logging import get_logger
from birdbrain.site_ocr import SiteOcrWatcher
from birdbrain.sites import Site
from birdbrain.storage import Database, SourceStateRow

log = get_logger(__name__)

# OCR results expire after this much time without a refresh — protects against
# stale state if the OCR thread dies silently.
OCR_TTL = timedelta(minutes=5)


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite throws away tz info on read. Everything we write is UTC, so
    re-attach UTC if a value comes back naive."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


@dataclass(slots=True)
class ResolvedSite:
    site: str | None
    latitude: float | None
    longitude: float | None
    detected_by: str | None  # "manual", "ocr", or None


class SiteResolver:
    def __init__(
        self,
        source: SourceConfig,
        sites: dict[str, Site],
        db: Database,
        ocr: SiteOcrWatcher | None = None,
    ) -> None:
        self.source = source
        self.sites = sites
        self.db = db
        self.ocr = ocr

    def current(self) -> ResolvedSite:
        # Single-site sources don't need any resolution work.
        if not self.source.multisite:
            return ResolvedSite(
                site=None,
                latitude=self.source.lat,
                longitude=self.source.lon,
                detected_by=None,
            )

        # 1. Manual override (DB-backed, set via web UI).
        state = self.db.get_source_state(self.source.name)
        now = datetime.now(UTC)
        manual_until = _as_utc(state.manual_until) if state else None
        if state and manual_until and manual_until > now and state.site:
            return ResolvedSite(
                site=state.site,
                latitude=state.latitude,
                longitude=state.longitude,
                detected_by="manual",
            )

        # 2. OCR result (in-memory, recent).
        if self.ocr is not None and self.ocr.latest is not None:
            site, when = self.ocr.latest
            if now - when < OCR_TTL:
                # Reflect the OCR finding into source_state so the dashboard sees it.
                if state is None or state.site != site.name or state.detected_by != "ocr":
                    self.db.set_source_state(
                        self.source.name,
                        site=site.name,
                        latitude=site.lat,
                        longitude=site.lon,
                        detected_by="ocr",
                        manual_until=None,
                    )
                return ResolvedSite(
                    site=site.name,
                    latitude=site.lat,
                    longitude=site.lon,
                    detected_by="ocr",
                )

        # 3. Fall back to source defaults; clear stale state if any.
        if state and state.detected_by is not None:
            self.db.clear_manual_override(self.source.name)
        return ResolvedSite(
            site=None,
            latitude=self.source.lat,
            longitude=self.source.lon,
            detected_by=None,
        )


def state_to_resolved(state: SourceStateRow | None, fallback: SourceConfig) -> ResolvedSite:
    """Convenience for the web app: read DB state without needing a resolver."""
    if state and state.site:
        now = datetime.now(UTC)
        manual_until = _as_utc(state.manual_until)
        active = (
            state.detected_by == "manual"
            and manual_until is not None
            and manual_until > now
        ) or state.detected_by == "ocr"
        if active:
            return ResolvedSite(
                site=state.site,
                latitude=state.latitude,
                longitude=state.longitude,
                detected_by=state.detected_by,
            )
    return ResolvedSite(
        site=None,
        latitude=fallback.lat,
        longitude=fallback.lon,
        detected_by=None,
    )
