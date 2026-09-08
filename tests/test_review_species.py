"""/review leads with a species index, not a flat list of near-duplicate clips."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database, DetectionRow
from birdbrain.web.app import create_app


def _app(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
        media_cache_enabled=False,
    )
    return create_app(cfg), Database(cfg.db_url)


def _listed(html: str) -> list[str]:
    """Scientific names the species index actually lists.

    Assert on these rather than on the whole page: base.html carries a global
    species search datalist holding every name ever heard, so a naive
    ``"X" in html`` is true for species the queue is not offering at all.
    """
    return re.findall(r"tab=detections&(?:amp;)?sci=([^\"&]+)", html)


def _add(db, *, sci, common, source="Cam", conf=0.8, n=1, clip=True, label=None, at=None):
    base = at or datetime.now(UTC)
    for i in range(n):
        db.insert_detections(
            [Detection(source_name=source, started_at=base + timedelta(seconds=3 * i),
                       duration_s=3.0, scientific_name=sci, common_name=common,
                       confidence=conf)],
            clip_path=f"/clips/{sci}-{source}-{i}.ogg" if clip else None,
        )
    if label:
        with db.session() as s, s.begin():
            for row in s.query(DetectionRow).filter(DetectionRow.scientific_name == sci):
                row.label = label


def test_the_default_tab_is_the_species_index(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="Pterocles namaqua", common="Namaqua Sandgrouse", n=35, conf=1.0)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", n=4, conf=0.9)
    html = TestClient(app).get("/review").text
    # One row per species, not one per clip: the 35-clip species appears once.
    listed = _listed(html)
    assert listed.count("Pterocles%20namaqua") == 1
    assert sorted(listed) == ["Otus%20senegalensis", "Pterocles%20namaqua"]
    assert "35" in html and "waiting" in html


def test_a_species_row_links_into_its_own_queue(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", n=3)
    html = TestClient(app).get("/review").text
    assert "Otus%20senegalensis" in _listed(html)

    drill = TestClient(app).get("/review?tab=detections&sci=Otus senegalensis").text
    assert "all species" in drill              # the way back
    assert "African Scops-Owl" in drill


def test_the_drill_down_is_an_exact_match_not_a_substring(tmp_path):
    """The free-text box is a substring match; the drill-down must not be, or
    'Otus' would drag in every scops-owl."""
    app, db = _app(tmp_path)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", n=2)
    _add(db, sci="Otus scops", common="Eurasian Scops-Owl", n=2)
    html = TestClient(app).get("/review?tab=detections&sci=Otus senegalensis").text
    rows = re.findall(r"id=\"audit-\d+\"", html)
    assert len(rows) == 2                       # only the two Otus senegalensis clips
    assert "Eurasian Scops-Owl" not in html.split("<ul")[-1]


def test_unreviewed_counts_only_clips_that_still_exist(tmp_path):
    """Retention prunes clips but keeps the rows. Counting every unlabelled row
    said 1.93M when only 720k could still be auditioned — a queue three times
    more hopeless than the real one."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Has clip", n=3, clip=True)
    _add(db, sci="B", common="Clip pruned", n=97, clip=False)
    assert db.review_backlog_total() == 3
    html = TestClient(app).get("/review?tab=detections").text
    assert "of 3 unreviewed" in html
    # ...and the pruned species is not offered for review at all.
    assert _listed(TestClient(app).get("/review").text) == ["A"]


def test_reviewed_species_drop_out_of_the_queue(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Done", n=2, label="good")
    _add(db, sci="B", common="Waiting", n=2)
    assert _listed(TestClient(app).get("/review").text) == ["B"]


def test_site_and_confidence_filters_narrow_the_species_index(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Loud Here", source="Cam1", conf=0.95, n=2)
    _add(db, sci="B", common="Quiet There", source="Cam2", conf=0.30, n=2)
    c = TestClient(app)
    assert _listed(c.get("/review?tab=species&source=Cam1").text) == ["A"]
    assert _listed(c.get("/review?tab=species&min_conf=0.5").text) == ["A"]


def test_the_filters_survive_the_click_through(tmp_path):
    """Picking a site then a species must not silently drop the site."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", n=2)
    html = TestClient(app).get("/review?tab=species&source=Cam1").text
    assert "A" in _listed(html)
    assert "source=Cam1" in html


def test_species_summary_reports_spread_and_reach(tmp_path):
    _, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", conf=0.30, n=2)
    _add(db, sci="A", common="Bird", source="Cam2", conf=0.95, n=3)
    row = db.review_species_summary()[0]
    assert row["n"] == 5
    assert row["sites"] == 2
    assert row["min_conf"] == 0.30 and row["max_conf"] == 0.95
    assert sum(row["bands"]) == 5          # every clip lands in exactly one band
    assert row["bands"][0] == 2 and row["bands"][-1] == 3


def test_ordering_options(tmp_path):
    _, db = _app(tmp_path)
    _add(db, sci="Few", common="Few", conf=0.99, n=1)
    _add(db, sci="Many", common="Many", conf=0.20, n=5)
    by_backlog = [r["scientific_name"] for r in db.review_species_summary(order="backlog")]
    by_conf = [r["scientific_name"] for r in db.review_species_summary(order="conf_desc")]
    assert by_backlog[0] == "Many"
    assert by_conf[0] == "Few"


def test_legacy_audition_still_lands_on_the_flat_list(tmp_path):
    app, _db = _app(tmp_path)
    r = TestClient(app).get("/audition", follow_redirects=False)
    assert r.status_code == 302
    assert "tab=detections" in r.headers["location"]
