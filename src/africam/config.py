from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class SourceConfig(BaseModel):
    name: str
    kind: Literal["youtube", "rtsp"]
    url: str
    lat: float | None = None
    lon: float | None = None
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # Week of year (1-48) used by BirdNET's location/time filter. None = current week.
    week: int | None = None


class AppConfig(BaseSettings):
    """Top-level config. Loaded from env (AFRICAM_*) and an optional .env file."""

    model_config = SettingsConfigDict(
        env_prefix="AFRICAM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sources_file: Path = Path("sources.toml")
    db_url: str = "sqlite:///data/africam.sqlite"
    clips_dir: Path = Path("data/clips")
    save_clips: bool = True
    sample_rate: int = 48_000
    chunk_seconds: float = 3.0
    log_level: str = "INFO"


def load_sources(path: Path) -> list[SourceConfig]:
    if not path.exists():
        raise FileNotFoundError(
            f"Sources file not found at {path}. Copy sources.example.toml to sources.toml."
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)
    entries = raw.get("source", [])
    return [SourceConfig.model_validate(e) for e in entries]
