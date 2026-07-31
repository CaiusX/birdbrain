from fastapi.testclient import TestClient
from birdbrain.config import AppConfig
from birdbrain.storage import Database
from birdbrain.web.app import create_app


def _app(tmp_path):
    cfg = AppConfig(db_url=f"sqlite:///{tmp_path/'r.sqlite'}", clips_dir=tmp_path/"c",
                    sources_file=tmp_path/"nope.toml", sites_file=tmp_path/"nope.toml",
                    media_cache_enabled=False)
    Database(cfg.db_url)
    return TestClient(create_app(cfg))


def test_robots_is_served_not_404(tmp_path):
    """It 404'd before, which is why 116 crawler probes a day got no policy."""
    r = _app(tmp_path).get("/robots.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")


def test_expensive_and_private_paths_are_disallowed(tmp_path):
    body = _app(tmp_path).get("/robots.txt").text
    for p in ("/admin", "/login", "/api/", "/ingest/", "/partials/",
              "/clips/", "/spectrograms/"):
        assert f"Disallow: {p}" in body, f"{p} should be disallowed"
    assert "Crawl-delay: 10" in body


def test_species_pages_stay_indexable(tmp_path):
    """The content pages are the reason to be indexed — blocking them to save
    CPU would trade away the point of a public site."""
    body = _app(tmp_path).get("/robots.txt").text
    assert "Disallow: /species" not in body
    assert "Disallow: /rare" not in body


def test_known_scrapers_are_banned(tmp_path):
    body = _app(tmp_path).get("/robots.txt").text
    for bot in ("DotBot", "SemrushBot", "AhrefsBot", "GPTBot", "Bytespider"):
        assert f"User-agent: {bot}" in body
