"""Confusion candidates for the audition pane's A/B comparison.

The panel answers "what else could this be", and the ranking is the whole
feature: a genus-mate recorded at the same camera is a live hypothesis, one
from the other end of the country is trivia.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
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


def _add(db, *, sci, common, source, n=1):
    """Insert n detections and return the id of the last one.

    ``insert_detections`` returns a row *count*, not ids, so read the id back
    rather than assuming the two coincide -- they only do while the subject of
    the test happens to be the first row inserted, which is exactly the kind of
    accident that makes a later test silently assert against the wrong row.
    """
    base = datetime.now(UTC)
    for i in range(n):
        db.insert_detections(
            [Detection(source_name=source, started_at=base + timedelta(seconds=3 * i),
                       duration_s=3.0, scientific_name=sci, common_name=common,
                       confidence=0.8)],
            clip_path=f"/c/{source}-{sci}-{i}.ogg",
        )
    with db.session() as s:
        return s.execute(
            sa_text("SELECT MAX(id) FROM detections WHERE scientific_name = :sci"),
            {"sci": sci},
        ).scalar_one()


def _similar(client, det_id):
    r = client.get(f"/api/detections/{det_id}/similar")
    assert r.status_code == 200, r.text
    return r.json()


class TestCandidates:
    def test_genus_mates_are_candidates_and_self_is_not(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Cisticola aridulus", common="Desert Cisticola", source="Cam")
        _add(db, sci="Cisticola natalensis", common="Croaking Cisticola", source="Other")

        body = _similar(c, det)
        names = [x["scientific_name"] for x in body["candidates"]]
        assert "Cisticola juncidis" not in names, "the species itself is not its own candidate"
        assert set(names) == {"Cisticola aridulus", "Cisticola natalensis"}
        assert body["genus"] == "Cisticola"

    def test_other_genera_are_not_candidates(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Ploceus velatus", common="Southern Masked-Weaver", source="Cam")

        assert _similar(c, det)["candidates"] == []

    def test_a_genus_prefix_is_not_a_genus(self, tmp_path):
        """``Apalis`` must not drag in ``Apalisoides``-style names — the match
        is on the genus token, not a string prefix."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Apalis flavida", common="Yellow-breasted Apalis", source="Cam")
        _add(db, sci="Apalisoides fictus", common="Not An Apalis", source="Cam")

        assert _similar(c, det)["candidates"] == []


class TestRanking:
    def test_heard_at_this_site_outranks_a_commoner_stranger(self, tmp_path):
        """THE ranking rule. The stranger has 50x the detections, but it has
        never occurred at this camera, so the local bird is the better
        hypothesis and must come first."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Cisticola aridulus", common="Desert Cisticola", source="Cam", n=1)
        _add(db, sci="Cisticola natalensis", common="Croaking Cisticola", source="Far", n=50)

        cands = _similar(c, det)["candidates"]
        assert [x["common_name"] for x in cands] == ["Desert Cisticola", "Croaking Cisticola"]
        assert cands[0]["here"] is True
        assert "heard at this site" in cands[0]["reason"]
        assert cands[1]["here"] is False

    def test_within_a_tier_the_commoner_bird_comes_first(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Cisticola aridulus", common="Desert Cisticola", source="Far", n=2)
        _add(db, sci="Cisticola natalensis", common="Croaking Cisticola", source="Far", n=9)

        cands = _similar(c, det)["candidates"]
        assert [x["common_name"] for x in cands] == ["Croaking Cisticola", "Desert Cisticola"]
        assert cands[0]["n"] == 9


class TestEdges:
    def test_missing_detection_is_404(self, tmp_path):
        app, _db = _app(tmp_path)
        assert TestClient(app).get("/api/detections/999999/similar").status_code == 404

    def test_resolves_the_subject_wherever_it_was_inserted(self, tmp_path):
        """Guards the fixture as much as the endpoint: the candidate list must
        come from the detection asked for, not from whichever row happens to
        be first in the table."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        _add(db, sci="Ploceus velatus", common="Southern Masked-Weaver", source="Cam")
        _add(db, sci="Ploceus intermedius", common="Lesser Masked-Weaver", source="Cam")
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Cisticola aridulus", common="Desert Cisticola", source="Cam")

        body = _similar(c, det)
        assert body["species"]["common_name"] == "Zitting Cisticola"
        assert [x["common_name"] for x in body["candidates"]] == ["Desert Cisticola"]

    def test_species_with_no_genus_mates_yields_an_empty_list(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Otus senegalensis", common="African Scops-Owl", source="Cam")
        body = _similar(c, det)
        assert body["candidates"] == []
        assert body["species"]["common_name"] == "African Scops-Owl"
