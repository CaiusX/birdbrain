"""One-off backfill: hit the local /api/species_image for every species in
species_notes so each gets its IUCN conservation_status populated in the DB.
A modest delay between calls keeps Wikipedia from rate-limiting us.

Run with: uv run python scripts/backfill_species_status.py
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request

from birdbrain.config import AppConfig
from birdbrain.storage import Database, SpeciesNoteRow

BASE = "http://127.0.0.1:8765"
DELAY = 1.5  # seconds between calls — image-list endpoint rate-limits hard


def main() -> int:
    db = Database(AppConfig().db_url)
    notes = db.list_species_notes()
    total = len(notes)
    ok = skip = miss = err = 0
    for i, n in enumerate(notes, 1):
        if n.conservation_status:
            skip += 1
            print(f"  [{i}/{total}] {n.common_name:<32} skip (already {n.conservation_status})")
            continue
        qs = urllib.parse.urlencode({"scientific": n.scientific_name, "common": n.common_name})
        try:
            with urllib.request.urlopen(f"{BASE}/api/species_image?{qs}", timeout=15) as resp:
                import json
                data = json.load(resp)
        except Exception as e:
            err += 1
            print(f"  [{i}/{total}] {n.common_name:<32} FAIL  {e}")
            continue
        status = data.get("conservation_status")
        if status:
            ok += 1
            print(f"  [{i}/{total}] {n.common_name:<32} -> {status}")
        else:
            miss += 1
            print(f"  [{i}/{total}] {n.common_name:<32} (no icon in WP article)")
        time.sleep(DELAY)
    print()
    print(f"backfill done: {ok} populated, {skip} already had, {miss} missing icon, {err} errors, total={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
