"""Central clip retention: the age window, per-species overrides, audited
clips, the low/mid/high reference set, and shared-file protection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from birdbrain.detector.birdnet import Detection
from birdbrain.retention import RetentionPolicy, protected_clip_paths, prune_clips
from birdbrain.storage import Database, DetectionRow

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class Store:
    """A DB plus a clips directory, with a helper to add a detection window."""

    def __init__(self, tmp_path):
        self.db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
        self.root = tmp_path / "clips"
        self.n = 0

    def add(self, *, source="Cam", species="Sp", conf=0.6, age_days=0.0, at=None,
            extra_species=(), label=None, rating=None, siblings=()):
        """One clip file shared by ``species`` and ``extra_species``."""
        at = at or (NOW - timedelta(days=age_days))
        day_dir = self.root / source / at.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        self.n += 1
        p = day_dir / f"{at.strftime('%Y%m%dT%H%M%S')}_{self.n:06d}Z.ogg"
        p.write_bytes(b"x" * 1000)
        for suffix in siblings:
            (day_dir / f"{p.stem}{suffix}").write_bytes(b"y" * 100)
        dets = [
            Detection(source_name=source, started_at=at, duration_s=3.0,
                      scientific_name=s, common_name=s, confidence=conf)
            for s in (species, *extra_species)
        ]
        self.db.insert_detections(dets, clip_path=str(p))
        if label or rating:
            with self.db.session() as s, s.begin():
                row = s.scalar(select(DetectionRow).where(DetectionRow.clip_path == str(p)))
                row.label = label
                row.sound_rating = rating
        return p

    def rows(self):
        with self.db.session() as s:
            return sorted(s.scalars(select(DetectionRow)), key=lambda r: r.id)


def test_expired_clips_go_and_recent_ones_stay(tmp_path):
    st = Store(tmp_path)
    old = st.add(species="A", age_days=40)
    fresh = st.add(species="A", age_days=5)
    # Make "A" have a newer file in the same band, so ``old`` is not the reference
    # (it would be otherwise: the reference set never expires).
    stats = prune_clips(st.db, RetentionPolicy(days=30), now=NOW)
    assert not old.exists() and fresh.exists()
    assert stats.files_deleted == 1 and stats.rows_nulled == 1
    assert stats.bytes_freed == 1000
    rows = st.rows()
    assert rows[0].clip_path is None and rows[1].clip_path == str(fresh)


def test_reference_set_keeps_newest_per_species_source_band_forever(tmp_path):
    st = Store(tmp_path)
    # Species A at Cam: two old low, one old mid, one old high. The newest of
    # each band survives; the older low goes.
    low_older = st.add(species="A", conf=0.3, age_days=60)
    low_newer = st.add(species="A", conf=0.4, age_days=50)
    mid = st.add(species="A", conf=0.6, age_days=55)
    high = st.add(species="A", conf=0.9, age_days=58)
    # Same species at another source has its own set.
    other = st.add(source="Cam2", species="A", conf=0.3, age_days=70)
    prune_clips(st.db, RetentionPolicy(days=30), now=NOW)
    assert not low_older.exists()
    assert low_newer.exists() and mid.exists() and high.exists() and other.exists()
    # A row whose file survived keeps pointing at it.
    kept = [r for r in st.rows() if r.clip_path]
    assert len(kept) == 4


def test_reference_set_can_be_disabled(tmp_path):
    st = Store(tmp_path)
    p = st.add(species="A", conf=0.9, age_days=60)
    prune_clips(st.db, RetentionPolicy(days=30, keep_reference_set=False), now=NOW)
    assert not p.exists()


def test_audited_clips_are_kept(tmp_path):
    st = Store(tmp_path)
    labelled = st.add(species="A", age_days=60, label="correct")
    rated = st.add(species="A", age_days=59, rating=4)
    plain = st.add(species="A", age_days=58)
    st.add(species="A", age_days=1)  # newest mid → the reference, so ``plain`` is expendable
    prune_clips(st.db, RetentionPolicy(days=30), now=NOW)
    assert labelled.exists() and rated.exists() and not plain.exists()


def test_a_shared_file_survives_if_any_row_is_protected(tmp_path):
    """One window, two species: B is labelled, A is not. The file stays, and
    A's row keeps its clip_path because the file is still there."""
    st = Store(tmp_path)
    shared = st.add(species="B", extra_species=("A",), age_days=60, label="correct")
    st.add(species="A", age_days=1)  # A's reference is elsewhere
    stats = prune_clips(st.db, RetentionPolicy(days=30), now=NOW)
    assert shared.exists()
    assert stats.rows_kept_shared_file == 1 and stats.files_deleted == 0
    old_rows = [r for r in st.rows() if r.started_at.replace(tzinfo=UTC) < NOW - timedelta(days=30)]
    assert len(old_rows) == 2 and all(r.clip_path == str(shared) for r in old_rows)


def test_species_override_expires_common_birds_faster(tmp_path):
    st = Store(tmp_path)
    goose_old = st.add(species="Alopochen aegyptiaca", age_days=5)
    goose_ref = st.add(species="Alopochen aegyptiaca", age_days=1)  # reference for the band
    other = st.add(species="Rare", age_days=5)
    policy = RetentionPolicy(days=30, species_days={"Alopochen aegyptiaca": 2})
    prune_clips(st.db, policy, now=NOW)
    assert not goose_old.exists()
    assert goose_ref.exists() and other.exists()


def test_override_still_keeps_the_reference_set_and_audited(tmp_path):
    st = Store(tmp_path)
    only = st.add(species="Alopochen aegyptiaca", conf=0.9, age_days=20)
    labelled = st.add(species="Alopochen aegyptiaca", conf=0.9, age_days=10, label="correct")
    prune_clips(st.db, RetentionPolicy(days=30, species_days={"Alopochen aegyptiaca": 1}), now=NOW)
    assert labelled.exists()
    # ``labelled`` is newer, so it is also the high-band reference; ``only`` has no protection.
    assert not only.exists()


def test_policy_from_db_overrides(tmp_path):
    st = Store(tmp_path)
    st.db.set_species_clip_retention_days("Bostrychia hagedash", 2)
    st.db.set_species_clip_retention_days("Corythaixoides concolor", 3)
    st.db.set_species_clip_retention_days("Corythaixoides concolor", None)
    assert st.db.species_clip_retention_map() == {"Bostrychia hagedash": 2}


def test_dry_run_counts_but_touches_nothing(tmp_path):
    st = Store(tmp_path)
    old = st.add(species="A", age_days=60, siblings=(".mp3", ".fire.png"))
    st.add(species="A", age_days=1)
    stats = prune_clips(st.db, RetentionPolicy(days=30), now=NOW, dry_run=True)
    assert stats.files_deleted == 1 and stats.rows_nulled == 1 and stats.siblings_deleted == 2
    assert stats.bytes_freed == 1000 + 200
    assert old.exists() and st.rows()[0].clip_path == str(old)


def test_cached_siblings_and_empty_day_dirs_are_removed(tmp_path):
    st = Store(tmp_path)
    old = st.add(species="A", age_days=60, siblings=(".mp3", ".fire.png", ".fire.large.png"))
    st.add(species="A", age_days=1)
    stats = prune_clips(st.db, RetentionPolicy(days=30), now=NOW)
    assert stats.siblings_deleted == 3
    assert not old.parent.exists()  # the day directory emptied out


def test_missing_file_still_nulls_the_row(tmp_path):
    st = Store(tmp_path)
    old = st.add(species="A", age_days=60)
    st.add(species="A", age_days=1)
    old.unlink()
    stats = prune_clips(st.db, RetentionPolicy(days=30), now=NOW)
    assert stats.files_missing == 1 and stats.rows_nulled == 1
    assert st.rows()[0].clip_path is None


def test_protected_paths_union(tmp_path):
    st = Store(tmp_path)
    a = st.add(species="A", conf=0.9, age_days=60, label="correct")
    b = st.add(species="A", conf=0.9, age_days=1)
    c = st.add(species="A", conf=0.3, age_days=70)
    assert protected_clip_paths(st.db, RetentionPolicy()) == {str(a), str(b), str(c)}


def test_empty_store_is_a_noop(tmp_path):
    st = Store(tmp_path)
    stats = prune_clips(st.db, RetentionPolicy(days=30), now=NOW)
    assert stats.days_scanned == 0 and stats.files_deleted == 0
