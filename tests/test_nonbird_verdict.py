"""Recording that a detection was not a bird at all.

BirdNET has no label for an African frog — 41 amphibian classes, every one
North American — so frogs, insects and traffic all land on birds. The free-text
suggestion box collected "Insects", "Hippo" and "Train": true about the clip,
uncountable afterwards. This is the countable version.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database, DetectionRow
from birdbrain.web.app import create_app


@pytest.fixture
def env(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
        media_cache_enabled=False,
    )
    app = create_app(cfg)
    db = Database(cfg.db_url)
    db.insert_detections(
        [Detection(source_name="Tau", started_at=datetime.now(UTC), duration_s=3.0,
                   scientific_name="Philetairus socius", common_name="Sociable Weaver",
                   confidence=0.31)],
        clip_path="/c/a.ogg",
    )
    with db.session() as s:
        det_id = s.query(DetectionRow).one().id
    uid = db.ensure_user("tester", role="tester") if hasattr(db, "ensure_user") else 1
    return app, db, det_id, uid


def _score(db, det_id, uid, label, **kw):
    return db.upsert_detection_score(det_id, uid, label, **kw)


class TestRecording:
    def test_a_rejection_can_name_what_it_actually_was(self, env):
        _app, db, det_id, uid = env
        assert _score(db, det_id, uid, "bad", nonbird="frog")
        with db.session() as s:
            assert s.get(DetectionRow, det_id).nonbird == "frog"

    def test_the_vocabulary_is_closed(self, env):
        """Free text is what made the existing 21 suggestions unusable."""
        _app, db, det_id, uid = env
        with pytest.raises(ValueError):
            _score(db, det_id, uid, "bad", nonbird="Hippo")

    def test_accepting_a_clip_clears_it(self, env):
        """'Correct ID' and 'it was a frog' cannot both be true."""
        _app, db, det_id, uid = env
        _score(db, det_id, uid, "bad", nonbird="frog")
        _score(db, det_id, uid, "good")
        with db.session() as s:
            assert s.get(DetectionRow, det_id).nonbird is None

    def test_clearing_the_label_clears_it(self, env):
        _app, db, det_id, uid = env
        _score(db, det_id, uid, "bad", nonbird="insect")
        _score(db, det_id, uid, None)
        with db.session() as s:
            assert s.get(DetectionRow, det_id).nonbird is None

    def test_it_survives_an_unrelated_edit(self, env):
        """Setting a sound rating must not wipe the reason for the rejection."""
        _app, db, det_id, uid = env
        _score(db, det_id, uid, "bad", nonbird="frog")
        _score(db, det_id, uid, "bad", sound_rating=4)
        with db.session() as s:
            row = s.get(DetectionRow, det_id)
            assert row.nonbird == "frog"
            assert row.sound_rating == 4


class TestApi:
    def test_an_unknown_kind_is_rejected_over_http(self, env):
        app, _db, det_id, _uid = env
        r = TestClient(app).post(f"/api/detections/{det_id}/label",
                                 data={"value": "bad", "nonbird": "dragon"})
        assert r.status_code in (400, 401), r.status_code

    def test_the_score_endpoint_reports_it(self, env):
        app, db, det_id, uid = env
        _score(db, det_id, uid, "bad", nonbird="frog")
        body = TestClient(app).get(f"/api/detections/{det_id}/score").json()
        assert "nonbird" in body


def test_it_is_queryable_which_was_the_whole_point(env):
    """The reason this is a column and not a sentence: you can count it."""
    _app, db, det_id, uid = env
    _score(db, det_id, uid, "bad", nonbird="frog")
    with db.session() as s:
        from sqlalchemy import func, select
        counts = dict(s.execute(
            select(DetectionRow.nonbird, func.count())
            .where(DetectionRow.nonbird.is_not(None))
            .group_by(DetectionRow.nonbird)
        ).all())
    assert counts == {"frog": 1}
