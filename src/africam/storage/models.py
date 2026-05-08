from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
