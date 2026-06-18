from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

# Windows consoles default to cp1252 under uv subprocesses, which can't encode
# the unicode block characters used in the summary sparkline. Reconfigure once
# at import so rich can emit utf-8 unimpeded.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
from rich.console import Console
from rich.table import Table
from sqlalchemy import desc, select

from africam.config import AppConfig, load_sources
from africam.cookies import refresh as refresh_cookies_impl
from africam.logging import configure as configure_logging
from africam.pipeline import run_all
from africam.storage import Database, DetectionRow, RuntimeSourceRow

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command()
def run(
    sources_file: Annotated[
        Path | None,
        typer.Option("--sources", "-s", help="Path to a sources.toml file."),
    ] = None,
) -> None:
    """Run the live detection pipeline against every configured source."""
    cfg = AppConfig()
    if sources_file is not None:
        cfg.sources_file = sources_file
    configure_logging(cfg.log_level)
    sources = load_sources(cfg.sources_file)
    if not sources:
        console.print("[red]No sources configured.[/red]")
        raise typer.Exit(code=1)
    run_all(sources, cfg)


@app.command(name="refresh-cookies")
def refresh_cookies(
    force: Annotated[
        bool, typer.Option("--force", help="Refresh even if no cam is bot-gated.")
    ] = False,
    min_interval_hours: Annotated[
        float, typer.Option("--min-interval-hours", help="Debounce window.")
    ] = 6.0,
    profile: Annotated[
        Path | None,
        typer.Option("--profile", help="Firefox profile dir (else auto-detect)."),
    ] = None,
) -> None:
    """Re-export the YouTube cookies file from Firefox when cams are bot-gated.

    Safe to run on a timer: by default it only acts when a YouTube worker is
    stalled on a "confirm you're not a bot" / stale-cookies error, and at most
    once per --min-interval-hours."""
    cfg = AppConfig()
    configure_logging(cfg.log_level)
    db = Database(cfg.db_url)
    result = refresh_cookies_impl(
        cfg, db,
        force=force,
        min_interval_h=min_interval_hours,
        profile=str(profile) if profile else None,
    )
    console.print(result)
    if result.get("action") in ("failed", "error"):
        raise typer.Exit(code=1)


@app.command()
def detections(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 25,
    source: Annotated[str | None, typer.Option("--source")] = None,
) -> None:
    """Show the most recent detections from the database."""
    cfg = AppConfig()
    db = Database(cfg.db_url)
    stmt = select(DetectionRow).order_by(desc(DetectionRow.started_at)).limit(limit)
    if source:
        stmt = stmt.where(DetectionRow.source_name == source)

    with db.session() as s:
        rows = list(s.scalars(stmt))

    table = Table(title=f"Recent detections (last {len(rows)})")
    table.add_column("Time (UTC)")
    table.add_column("Source")
    table.add_column("Species")
    table.add_column("Scientific", style="dim")
    table.add_column("Conf", justify="right")
    for r in rows:
        table.add_row(
            r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            r.source_name,
            r.common_name,
            r.scientific_name,
            f"{r.confidence:.2f}",
        )
    console.print(table)


@app.command()
def summary(
    hours: Annotated[int, typer.Option("--hours", "-H", help="Look-back window in hours.")] = 24,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Restrict to one or more sources (repeatable)."),
    ] = None,
    top: Annotated[int, typer.Option("--top", help="Top N species per source.")] = 8,
    samples: Annotated[int, typer.Option("--samples", help="Clip samples per confidence bucket.")] = 2,
) -> None:
    """Per-source rollup + top species + sample clip paths for spot-checking."""
    cfg = AppConfig()
    db = Database(cfg.db_url)

    tz_by_source: dict[str, str] = {}
    try:
        for s_cfg in load_sources(cfg.sources_file):
            tz_by_source[s_cfg.name] = s_cfg.timezone
    except FileNotFoundError:
        pass
    with db.session() as s:
        for row in s.scalars(select(RuntimeSourceRow)):
            tz_by_source.setdefault(row.name, row.timezone or "UTC")

    since = datetime.now(UTC) - timedelta(hours=hours)
    base = select(DetectionRow).where(DetectionRow.started_at >= since)
    if source:
        base = base.where(DetectionRow.source_name.in_(source))

    with db.session() as s:
        rows: list[DetectionRow] = list(s.scalars(base))

    if not rows:
        console.print(f"[yellow]No detections in the last {hours}h.[/yellow]")
        return

    console.rule(f"[bold]Last {hours}h[/bold]  ({since.strftime('%Y-%m-%d %H:%MZ')} → now)  •  {len(rows)} detections")

    by_source: dict[str, list[DetectionRow]] = {}
    for r in rows:
        by_source.setdefault(r.source_name, []).append(r)

    rollup = Table(title="Per-source rollup")
    rollup.add_column("Source")
    rollup.add_column("Count", justify="right")
    rollup.add_column("Species", justify="right")
    rollup.add_column("Mean conf", justify="right")
    rollup.add_column(f"Hourly (last {hours}h)", style="cyan")
    spark_chars = " ▁▂▃▄▅▆▇█"
    for src_name, src_rows in sorted(by_source.items()):
        species = {r.scientific_name for r in src_rows}
        mean_conf = sum(r.confidence for r in src_rows) / len(src_rows)
        buckets = [0] * hours
        for r in src_rows:
            idx = int((r.started_at.replace(tzinfo=UTC) - since).total_seconds() // 3600)
            if 0 <= idx < hours:
                buckets[idx] += 1
        peak = max(buckets) or 1
        spark = "".join(spark_chars[min(len(spark_chars) - 1, int(b / peak * (len(spark_chars) - 1)))] for b in buckets)
        rollup.add_row(src_name, str(len(src_rows)), str(len(species)), f"{mean_conf:.2f}", spark)
    console.print(rollup)

    for src_name, src_rows in sorted(by_source.items()):
        tz = ZoneInfo(tz_by_source.get(src_name, "UTC"))
        species_stats: dict[str, dict] = {}
        for r in src_rows:
            d = species_stats.setdefault(
                r.scientific_name,
                {"common": r.common_name, "count": 0, "max": 0.0, "max_row": r},
            )
            d["count"] += 1
            if r.confidence > d["max"]:
                d["max"] = r.confidence
                d["max_row"] = r
        ordered = sorted(species_stats.values(), key=lambda d: d["count"], reverse=True)[:top]

        t = Table(title=f"[bold]{src_name}[/bold] — top {len(ordered)} species  (tz: {tz.key})")
        t.add_column("Species")
        t.add_column("Scientific", style="dim")
        t.add_column("Count", justify="right")
        t.add_column("Max conf", justify="right")
        t.add_column("Best clip (local time)")
        for d in ordered:
            best: DetectionRow = d["max_row"]
            local = best.started_at.replace(tzinfo=UTC).astimezone(tz)
            clip = best.clip_path or "—"
            t.add_row(
                d["common"],
                best.scientific_name,
                str(d["count"]),
                f"{d['max']:.2f}",
                f"{local.strftime('%m-%d %H:%M')}  {clip}",
            )
        console.print(t)

        buckets_def = [
            ("low ", lambda c: c < 0.6),
            ("mid ", lambda c: 0.6 <= c < 0.8),
            ("high", lambda c: c >= 0.8),
        ]
        audit = Table(title=f"{src_name} — audition samples")
        audit.add_column("Bucket")
        audit.add_column("Conf", justify="right")
        audit.add_column("Species")
        audit.add_column("Local time")
        audit.add_column("Clip", style="dim")
        for label, pred in buckets_def:
            picks = [r for r in src_rows if r.clip_path and pred(r.confidence)]
            if not picks:
                audit.add_row(label, "—", "—", "—", "(none)")
                continue
            step = max(1, len(picks) // samples)
            for r in picks[::step][:samples]:
                local = r.started_at.replace(tzinfo=UTC).astimezone(tz)
                audit.add_row(
                    label,
                    f"{r.confidence:.2f}",
                    r.common_name,
                    local.strftime("%m-%d %H:%M:%S"),
                    r.clip_path or "",
                )
        console.print(audit)


@app.command()
def probe(
    sources_file: Annotated[
        Path | None,
        typer.Option("--sources", "-s"),
    ] = None,
    seconds: Annotated[int, typer.Option("--seconds", "-t")] = 9,
) -> None:
    """Pull a few chunks from each configured source and run BirdNET once. Useful for smoke testing."""
    from africam.detector import BirdNetDetector
    from africam.pipeline import build_source

    cfg = AppConfig()
    if sources_file is not None:
        cfg.sources_file = sources_file
    configure_logging(cfg.log_level)
    sources = load_sources(cfg.sources_file)

    detector = BirdNetDetector()
    n_chunks = max(1, int(seconds // cfg.chunk_seconds))

    for src_cfg in sources:
        console.rule(f"[bold]{src_cfg.name}[/bold]")
        source = build_source(src_cfg, cfg)
        stream = source.stream()
        for i in range(n_chunks):
            chunk = next(stream, None)
            if chunk is None:
                console.print("[red]stream ended early[/red]")
                break
            dets = detector.analyze(
                chunk,
                lat=src_cfg.lat,
                lon=src_cfg.lon,
                week=src_cfg.week,
                min_confidence=src_cfg.min_confidence,
            )
            console.print(f"chunk {i+1}/{n_chunks}: {len(dets)} detections")
            for d in dets:
                console.print(
                    f"  {d.common_name} ({d.scientific_name})  conf={d.confidence:.2f}"
                )


@app.command()
def prune(
    days: Annotated[int, typer.Option("--days", "-d", help="Delete clip files older than this many days.")] = 14,
    keep_labelled: Annotated[bool, typer.Option("--keep-labelled/--no-keep-labelled")] = True,
    keep_pngs: Annotated[bool, typer.Option("--keep-pngs/--delete-pngs", help="Keep cached spectrogram PNGs (they regenerate on demand).")] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the y/N confirmation prompt.")] = False,
) -> None:
    """Delete old detection clips to free disk. DB rows are kept; clip_path is
    NULLed for any row whose audio file gets removed."""
    cfg = AppConfig()
    db = Database(cfg.db_url)

    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(DetectionRow.id, DetectionRow.clip_path, DetectionRow.label, DetectionRow.source_name, DetectionRow.confidence)
        .where(DetectionRow.started_at < cutoff)
        .where(DetectionRow.clip_path.is_not(None))
    )
    if keep_labelled:
        stmt = stmt.where(DetectionRow.label.is_(None))

    with db.session() as s:
        candidates = list(s.execute(stmt))

    # Group by clip_path so we delete each file once and NULL out every row
    # that references it.
    by_clip: dict[str, list[int]] = {}
    by_source: dict[str, int] = {}
    for row in candidates:
        by_clip.setdefault(row.clip_path, []).append(row.id)
        by_source[row.source_name] = by_source.get(row.source_name, 0) + 1

    total_bytes = 0
    missing = 0
    for clip in by_clip:
        p = Path(clip)
        if p.is_file():
            total_bytes += p.stat().st_size
            if not keep_pngs:
                for png in (p.with_suffix(".png"), p.parent / f"{p.stem}.large.png"):
                    if png.is_file():
                        total_bytes += png.stat().st_size
        else:
            missing += 1

    summary = Table(title=f"Prune candidates (older than {days}d)")
    summary.add_column("metric")
    summary.add_column("value", justify="right")
    summary.add_row("rows affected", f"{len(candidates):,}")
    summary.add_row("distinct clips", f"{len(by_clip):,}")
    summary.add_row("missing on disk", f"{missing:,}")
    summary.add_row("would free", f"{total_bytes/1024/1024:.1f} MB")
    summary.add_row("keep labelled", "yes" if keep_labelled else "no")
    summary.add_row("keep PNG cache", "yes" if keep_pngs else "no")
    console.print(summary)
    if by_source:
        per_src = Table(title="By source")
        per_src.add_column("source")
        per_src.add_column("rows", justify="right")
        for src in sorted(by_source):
            per_src.add_row(src, f"{by_source[src]:,}")
        console.print(per_src)

    if not by_clip:
        console.print("[green]nothing to prune.[/green]")
        return

    if not yes:
        ans = typer.prompt("delete? [y/N]", default="N", show_default=False)
        if ans.strip().lower() not in {"y", "yes"}:
            console.print("[yellow]aborted.[/yellow]")
            raise typer.Exit(code=1)

    deleted_files = 0
    deleted_bytes = 0
    nulled_rows = 0
    with db.session() as s, s.begin():
        for clip, ids in by_clip.items():
            p = Path(clip)
            if p.is_file():
                deleted_bytes += p.stat().st_size
                p.unlink()
                deleted_files += 1
            if not keep_pngs:
                for png in (p.with_suffix(".png"), p.parent / f"{p.stem}.large.png"):
                    if png.is_file():
                        deleted_bytes += png.stat().st_size
                        png.unlink()
            for det_id in ids:
                row = s.get(DetectionRow, det_id)
                if row is not None:
                    row.clip_path = None
                    nulled_rows += 1

    console.print(
        f"[green]deleted {deleted_files:,} clip files[/green] "
        f"({deleted_bytes/1024/1024:.1f} MB), "
        f"NULLed clip_path on {nulled_rows:,} rows."
    )


@app.command()
def web(
    host: Annotated[str, typer.Option("--host", "-H")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p")] = 8000,
    reload: Annotated[bool, typer.Option("--reload")] = False,
) -> None:
    """Run the web dashboard. Reads from the same SQLite as the pipeline."""
    import uvicorn

    cfg = AppConfig()
    configure_logging(cfg.log_level)
    uvicorn.run(
        "africam.web.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level=cfg.log_level.lower(),
    )


@app.command(name="dedup-backfill")
def dedup_backfill(
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Hash detections newer than this many days."),
    ] = 30,
    batch_size: Annotated[
        int,
        typer.Option("--batch-size", help="Rows to update per DB transaction."),
    ] = 200,
) -> None:
    """Compute audio_hash for existing detections so the replay filter can bite.

    Walks ``detections`` rows where audio_hash IS NULL and clip_path IS NOT
    NULL within the lookback, decoding each saved clip with
    ``africam.audio_hash.clip_hash``. Idempotent — rerunnable, will only
    touch rows that still lack a hash.
    """
    from africam.audio_hash import clip_hash

    cfg = AppConfig()
    configure_logging(cfg.log_level)
    db = Database(cfg.db_url)

    console.print(
        f"[cyan]Backfilling audio_hash for detections in the last {days} days "
        f"(batch={batch_size})…[/cyan]"
    )

    last_print = [0]

    def _progress(report: dict, total: int) -> None:
        # Print every batch end. Cheap (terminal can absorb 100s/s) and the
        # backfill is long enough that silence would feel like a hang.
        last_print[0] = report["processed"]
        console.print(
            f"  {report['processed']:>6,} / {total:,} "
            f"hashed={report['hashed']:,} "
            f"miss={report['missing_clip']+report['missing_file']:,} "
            f"err={report['errors']:,}",
            highlight=False,
        )

    report = db.backfill_audio_hash(
        days=days, batch_size=batch_size, hasher=clip_hash, progress_cb=_progress,
    )
    console.print(
        f"[green]done.[/green] "
        f"processed={report['processed']:,} "
        f"hashed={report['hashed']:,} "
        f"missing_clip={report['missing_clip']:,} "
        f"missing_file={report['missing_file']:,} "
        f"errors={report['errors']:,}"
    )


@app.command(name="set-password")
def set_password(
    username: Annotated[str, typer.Argument(help="Account username (created if missing).")],
    password: Annotated[
        str | None,
        typer.Option("--password", help="Password (prompted, hidden, if omitted)."),
    ] = None,
    operator: Annotated[
        bool,
        typer.Option("--operator", help="Create as the operator account if missing."),
    ] = False,
) -> None:
    """Set (or reset) a reviewer's password. Creates the user if they don't
    exist. Use this to give the migrated ``operator`` account a password, or to
    reset a tester's."""
    from africam.web import auth as auth_mod

    cfg = AppConfig()
    db = Database(cfg.db_url)
    uname = auth_mod.normalize_username(username)
    if not auth_mod.valid_username(uname):
        console.print("[red]Invalid username (3–64 chars: letters, digits, . _ -).[/red]")
        raise typer.Exit(1)
    if password is None:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    err = auth_mod.validate_password_rules(password)
    if err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1)
    existing = db.get_user_by_username(uname)
    role = "operator" if (operator or uname == "operator") else "tester"
    wrote = db.set_user_password(
        uname, auth_mod.hash_password(password),
        create_role=(None if existing else role),
    )
    if not wrote:
        console.print("[red]User not found.[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]Password {'updated' if existing else f'set (new {role})'} "
        f"for {uname!r}.[/green]"
    )


if __name__ == "__main__":
    app()
