"""Auto-refresh the YouTube cookies file from a local Firefox profile.

YouTube periodically challenges yt-dlp with "Sign in to confirm you're not a
bot" once the exported cookies age out, which drops the affected cams into
backoff. This re-exports a fresh copy from Firefox's ``cookies.sqlite`` (the
browser need not be running) and writes it atomically so the running workers
pick it up on their next retry — no pipeline restart needed.

Reactive + debounced on purpose: re-exporting from a logged-in browser makes
YouTube rotate the session, so we only act when a cam is actually bot-gated
and not more than once per ``min_interval_h``.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import structlog

from birdbrain.audio.youtube import _detect_js_runtime
from birdbrain.config import AppConfig, load_sources
from birdbrain.storage import Database

log = structlog.get_logger("birdbrain.cookies")

# yt-dlp / worker error fragments that mean "cookies are stale → bot-gated".
_BOT_GATE_RE = re.compile(
    r"sign in to confirm|not a bot|cookies are no longer valid",
    re.IGNORECASE,
)

# Where Firefox keeps profiles across Debian/standard, snap and flatpak installs.
_PROFILE_GLOBS = (
    "~/.mozilla/firefox/*/cookies.sqlite",
    "~/.config/mozilla/firefox/*/cookies.sqlite",
    "~/snap/firefox/common/.mozilla/firefox/*/cookies.sqlite",
    "~/.var/app/org.mozilla.firefox/.mozilla/firefox/*/cookies.sqlite",
)


def _ytdlp_bin() -> str:
    """Resolve the yt-dlp executable: PATH first (it's there under ``uv run``),
    else the one alongside the current interpreter (the project venv)."""
    return shutil.which("yt-dlp") or str(Path(sys.executable).parent / "yt-dlp")


def find_firefox_profile() -> Path | None:
    """The most-recently-used Firefox profile directory that has a
    cookies.sqlite, or None. yt-dlp's own auto-detect misses non-standard
    locations (e.g. ~/.config/mozilla on some Pi setups), so we resolve it."""
    best: Path | None = None
    best_mtime = -1.0
    for pattern in _PROFILE_GLOBS:
        for path in glob.glob(os.path.expanduser(pattern)):
            mtime = os.path.getmtime(path)
            if mtime > best_mtime:
                best_mtime = mtime
                best = Path(path).parent
    return best


def _youtube_targets(cfg: AppConfig, db: Database) -> tuple[Path | None, str | None]:
    """Pick the shared cookies file and one stream URL to validate against,
    from the YouTube sources (static + runtime) that use a cookies file."""
    cookies_file: Path | None = None
    url: str | None = None
    try:
        static = load_sources(cfg.sources_file)
    except FileNotFoundError:
        static = []
    for s in static:
        if s.kind == "youtube" and s.cookies_file:
            cookies_file = cookies_file or Path(s.cookies_file)
            url = url or s.url
    if cookies_file is None:
        for r in db.list_runtime_sources():
            if r.kind == "youtube" and r.cookies_file:
                cookies_file = Path(r.cookies_file)
                url = r.url
                break
    return cookies_file, url


def bot_gated_sources(db: Database) -> list[str]:
    """Source names whose worker is currently stalled on a YouTube bot-gate /
    stale-cookies error."""
    out: list[str] = []
    for h in db.list_worker_heartbeats():
        if (
            h.state in ("backoff", "stale", "error")
            and h.last_error
            and _BOT_GATE_RE.search(h.last_error)
        ):
            out.append(h.source_name)
    return out


def refresh(
    cfg: AppConfig,
    db: Database,
    *,
    force: bool = False,
    min_interval_h: float = 6.0,
    profile: str | None = None,
) -> dict:
    """Re-export the cookies file from Firefox if a cam is bot-gated (or
    ``force``). Returns a small result dict describing what happened."""
    cookies_file, url = _youtube_targets(cfg, db)
    if cookies_file is None or url is None:
        return {"action": "skip", "reason": "no youtube source with a cookies file"}

    gated = bot_gated_sources(db)
    if not force:
        if not gated:
            return {"action": "skip", "reason": "no bot-gated cams"}
        stamp = cookies_file.with_suffix(cookies_file.suffix + ".refreshed")
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < min_interval_h * 3600:
            return {"action": "skip", "reason": "debounced", "gated": gated}

    prof = Path(profile) if profile else find_firefox_profile()
    if prof is None or not prof.exists():
        log.warning("cookies.no_profile")
        return {"action": "error", "reason": "no firefox profile found"}

    # Export to a temp file, validate it actually resolves a stream, then
    # atomically swap it in so workers never read a half-written file.
    fd, tmp = tempfile.mkstemp(suffix=".cookies.txt", dir=str(cookies_file.parent))
    os.close(fd)
    # yt-dlp tries to LOAD an existing --cookies file before writing; an empty
    # temp fails its Netscape-format check, so hand it a free path to create.
    Path(tmp).unlink(missing_ok=True)
    cmd = [
        _ytdlp_bin(),
        "--cookies-from-browser", f"firefox:{prof}",
        "--cookies", tmp,
        "-f", "bestaudio/best", "-g", "--no-warnings",
    ]
    runtime = _detect_js_runtime()  # match the pipeline's resolve so validation is faithful
    if runtime:
        cmd += ["--js-runtimes", runtime]
    cmd.append(url)
    ok = False
    reason = ""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        ok = (
            res.returncode == 0
            and "http" in res.stdout
            and Path(tmp).exists()
            and Path(tmp).stat().st_size > 1000
        )
        if not ok:
            tail = (res.stderr or res.stdout or "").strip().splitlines()[-1:] or [""]
            reason = tail[0][:200]
    except (OSError, subprocess.SubprocessError) as e:
        reason = str(e)[:200]
    if not ok:
        Path(tmp).unlink(missing_ok=True)
        log.warning("cookies.refresh_failed", err=reason)
        return {"action": "failed", "reason": reason, "gated": gated}

    os.replace(tmp, cookies_file)
    cookies_file.with_suffix(cookies_file.suffix + ".refreshed").touch()
    log.info("cookies.refreshed", gated=gated, profile=str(prof), file=str(cookies_file))
    return {"action": "refreshed", "gated": gated, "cookies_file": str(cookies_file)}
