from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class Site(BaseModel):
    """A named location with coordinates and OCR-matching aliases."""

    name: str
    lat: float
    lon: float
    # Strings the OCR resolver compares against detected on-screen text.
    # The site name is implicitly an alias; add abbreviations or alternate
    # spellings here. Matching is case-insensitive substring.
    aliases: list[str] = Field(default_factory=list)

    @property
    def all_terms(self) -> list[str]:
        return [self.name, *self.aliases]


def load_sites(path: Path) -> dict[str, Site]:
    """Load sites.toml. Returns an empty dict if the file is missing.

    Schema:

        [[site]]
        name = "Olifants"
        lat = -24.20
        lon = 30.92
        aliases = ["Olifants West", "Naledi"]
    """
    if not path.exists():
        return {}
    with path.open("rb") as f:
        raw = tomllib.load(f)
    out: dict[str, Site] = {}
    for entry in raw.get("site", []):
        s = Site.model_validate(entry)
        out[s.name] = s
    return out
