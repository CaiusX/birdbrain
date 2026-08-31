"""The public display cutoff.

BirdNET's low-confidence tail is mostly wrong, and a listed species reads to a
visitor as a claim. So reporting is held to a floor — 0.70 by default.

The important property is that it is a *display* filter and not a detector
floor. Those look interchangeable and are not: at 0.70 on this dataset, 56% of
rows and 209 of 679 species fall below the line, and 98 of those rows are
detections a human had already confirmed correct. Hiding them is a judgement
call that can be revisited; discarding them is not. Several tests below exist
only to keep that distinction from eroding.

/confidence and the review surfaces are deliberately exempt — one exists to
show what different cutoffs do to the roster, the other to judge exactly the
marginal calls this hides.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database, DetectionRow
from birdbrain.storage.db import REPORTING_MIN_CONFIDENCE_DEFAULT
from birdbrain.web.app import create_app

HIGH, LOW = "Certus birdus", "Dubius birdus"


def _app(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope.toml",
        sites_file=tmp_path / "nope.toml",
        media_cache_enabled=False,
    )
    db = Database(cfg.db_url)
    db.add_runtime_source(
        name="A", kind="device", url="pulse:x", lat=-26.1, lon=28.0,
        min_confidence=0.1, timezone="UTC",
    )
    now = datetime.now(UTC)
    db.insert_detections([
        Detection("A", now - timedelta(minutes=i), 3.0, HIGH, "Certain Bird", 0.95)
        for i in range(3)
    ] + [
        Detection("A", now - timedelta(minutes=10 + i), 3.0, LOW, "Doubtful Bird", 0.35)
        for i in range(3)
    ])
    return create_app(cfg), db


def test_default_is_seventy_percent(tmp_path):
    _, db = _app(tmp_path)
    assert db.reporting_min_confidence() == REPORTING_MIN_CONFIDENCE_DEFAULT == 0.70


def test_a_missing_or_broken_setting_falls_back_to_the_default(tmp_path):
    """Not to zero. A public page defaulting open because a settings row is
    malformed is the wrong way round for this to fail."""
    _, db = _app(tmp_path)
    db.set_setting("reporting_min_confidence", "not-a-number")
    assert db.reporting_min_confidence() == REPORTING_MIN_CONFIDENCE_DEFAULT
    db.set_setting("reporting_min_confidence", "5.0")     # out of range
    assert db.reporting_min_confidence() == REPORTING_MIN_CONFIDENCE_DEFAULT


def test_low_confidence_species_is_hidden_from_public_pages(tmp_path):
    """The pages that name species keep the confident one and drop the other.

    Includes the spectrogram modal's autocomplete, which renders on public
    pages and would otherwise put every sub-cutoff species into the page source
    of a site that shows none of them.
    """
    app, _ = _app(tmp_path)
    client = TestClient(app)
    for path in ("/", "/species"):
        body = client.get(path).text
        assert "Certain Bird" in body or HIGH in body, f"{path} lost the good species"
        assert "Doubtful Bird" not in body and LOW not in body, f"{path} leaked a 0.35 call"


def test_site_index_counts_respect_the_cutoff(tmp_path):
    """/sites names no species — it reports per-site counts, so that is what has
    to be filtered. Counting a detection the site will not show you is its own
    kind of wrong.

    The fixture has one site with 3 detections at 0.95 and 3 at 0.35, so the
    numbers are unambiguous: 3 above the default cutoff, 6 with it dropped.
    """
    app, db = _app(tmp_path)
    client = TestClient(app)

    def counts() -> set[int]:
        body = client.get("/sites").text
        return {int(n) for n in re.findall(r'data-sort="(\d+)"', body)}

    assert 3 in counts(), "expected the 3 confident detections to be counted"
    assert 6 not in counts(), "all 6 counted — the cutoff is not reaching /sites"

    db.set_reporting_min_confidence(0.0)
    assert 6 in counts(), "dropping the cutoff should reveal all 6"


def test_lowering_the_cutoff_brings_it_straight_back(tmp_path):
    """The property that makes this safe: nothing was deleted, so the filter is
    reversible in both directions."""
    app, db = _app(tmp_path)
    client = TestClient(app)
    assert "Doubtful Bird" not in client.get("/species").text
    db.set_reporting_min_confidence(0.0)
    assert "Doubtful Bird" in client.get("/species").text
    db.set_reporting_min_confidence(0.70)
    assert "Doubtful Bird" not in client.get("/species").text


def test_the_detector_floor_is_left_alone(tmp_path):
    """The display cutoff must not touch what gets recorded. If these two ever
    become the same setting, raising the cutoff starts destroying data."""
    _, db = _app(tmp_path)
    db.set_reporting_min_confidence(0.9)
    assert db.global_min_confidence() is None
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(DetectionRow)) == 6


def test_confidence_page_ignores_the_cutoff(tmp_path):
    """Its entire subject is what different floors do to the roster. Filtering
    it by the current floor would make it show a flat line."""
    app, db = _app(tmp_path)
    db.set_reporting_min_confidence(0.9)
    body = TestClient(app).get("/confidence").text
    assert '"confMin": 0.1' in body, "curve should still start below the cutoff"
    # the low-confidence rows must still be counted somewhere in the curves
    counts = [int(n) for n in re.findall(r"\[(\d+)", body)]
    assert any(c >= 6 for c in counts), "sub-cutoff detections vanished from the curves"


def test_review_surface_ignores_the_cutoff(tmp_path):
    """Reviewing is how a marginal call gets confirmed or rejected; hiding the
    marginal calls from it would defeat the workflow that improves accuracy."""
    app, db = _app(tmp_path)
    db.set_reporting_min_confidence(0.9)
    assert TestClient(app).get("/review").status_code == 200


def test_admin_endpoint_sets_and_validates(tmp_path):
    app, db = _app(tmp_path)
    client = TestClient(app)
    assert client.post("/api/settings/reporting-min-confidence",
                       data={"value": "0.5"}).status_code == 200
    assert db.reporting_min_confidence() == 0.5
    # empty restores the default rather than clearing to "show everything"
    client.post("/api/settings/reporting-min-confidence", data={"value": ""})
    assert db.reporting_min_confidence() == REPORTING_MIN_CONFIDENCE_DEFAULT
    for bad in ("2", "-1", "abc"):
        assert client.post("/api/settings/reporting-min-confidence",
                           data={"value": bad}).status_code == 400
