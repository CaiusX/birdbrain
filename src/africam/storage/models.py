from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DetectionRow(Base):
    __tablename__ = "detections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_s: Mapped[float] = mapped_column(Float)
    scientific_name: Mapped[str] = mapped_column(String(256), index=True)
    common_name: Mapped[str] = mapped_column(String(256))
    confidence: Mapped[float] = mapped_column(Float)
    clip_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Site that the source camera was pointed at when this detection happened.
    # NULL for single-site sources or when the resolver couldn't determine it.
    site: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Manual audition label: 'good' | 'bad' | 'unsure' | NULL (unreviewed).
    label: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When label='bad', the rater can suggest what the call actually was.
    # Free-text (auto-completed from species we've already detected). NULL
    # whenever label is anything other than 'bad'.
    suggested_species: Mapped[str | None] = mapped_column(String(256), nullable=True)

    __table_args__ = (
        Index("ix_detections_source_time", "source_name", "started_at"),
    )


class RuntimeSourceRow(Base):
    """Sources added at runtime via the web UI. Picked up by the pipeline
    supervisor without a restart. Soft-deleted (deleted_at != NULL) so the
    supervisor can stop the worker without losing history.

    Fields mirror :class:`SourceConfig` (the file-based equivalent) but live
    in SQL so the web app can mutate them without touching files.
    """

    __tablename__ = "runtime_sources"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # "youtube" | "rtsp"
    url: Mapped[str] = mapped_column(String(1024))
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    multisite: Mapped[bool] = mapped_column(default=False)
    cookies_from_browser: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cookies_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SpeciesNoteRow(Base):
    """A short curated comment about a species — displayed in the audition
    modal whenever a detection of that species is opened. Used to flag known
    false-positive-prone species, biome plausibility, ID confusions, etc.
    Keyed by scientific name since BirdNET reports it consistently."""

    __tablename__ = "species_notes"

    scientific_name: Mapped[str] = mapped_column(String(256), primary_key=True)
    common_name: Mapped[str] = mapped_column(String(256))
    note: Mapped[str] = mapped_column(Text)
    # Optional flag that styles the banner in the modal.
    # 'reliable' (green) | 'suspect' (amber) | 'rare' (blue) | NULL (neutral)
    tag: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # IUCN Red List code (LC/NT/VU/EN/CR/EW/EX/DD/NE), populated from the
    # Wikipedia article's status icon. NULL until first looked up.
    conservation_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Per-species detection threshold override. When set, the pipeline drops
    # detections of this species below this confidence even if the source's
    # min_confidence is lower. Lets us suppress loud common species (Egyptian
    # Goose, Hadada Ibis) that otherwise drown out quieter detections.
    min_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Provenance for AI-generated notes. NULL = curated/manual note (or no
    # note yet). When present, the background notes worker is allowed to
    # regenerate; when NULL but note is non-empty, the worker leaves it
    # alone so manual edits stick.
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Hash of the evidence inputs at the time of last generation. The worker
    # uses this to detect when the underlying detection profile has shifted
    # enough to be worth regenerating before the age-based trigger fires.
    evidence_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detection_count_at_gen: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WorkerHeartbeatRow(Base):
    """Liveness signal written by each per-source worker thread. The web
    /admin page reads this to tell which sources are actually producing data
    right now (vs configured but stopped/dead)."""

    __tablename__ = "worker_heartbeats"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    # When the current worker incarnation started — bumped each time a new
    # thread takes over for this source.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Touched ~every 15s by the worker while it's processing chunks.
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 'running' | 'backoff' (between retries) | 'stopped'
    state: Mapped[str] = mapped_column(String(16), default="running")
    # Last error message that triggered backoff (truncated). NULL when healthy.
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)


class SourceStateRow(Base):
    """Mutable per-source runtime state. The pipeline reads it on every chunk
    and the web app writes manual overrides to it. Used to coordinate the
    "which site is the camera at right now?" answer across processes."""

    __tablename__ = "source_state"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    site: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "manual" / "ocr" / None — how the current site was determined.
    detected_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # If set, manual overrides remain authoritative until this timestamp.
    manual_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
