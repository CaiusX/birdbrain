from __future__ import annotations

import sys
import threading
from collections.abc import Iterable
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
from sqlalchemy import desc, func, select

from birdbrain.config import AppConfig, load_sources
from birdbrain.cookies import refresh as refresh_cookies_impl
from birdbrain.logging import configure as configure_logging
from birdbrain.retention import RetentionPolicy, prune_clips
from birdbrain.storage import Database, DetectionRow, RuntimeSourceRow

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
    # Imported here, not at module scope: it pulls in the detector and with it
    # TensorFlow (~250MB resident). Every subcommand pays that on a module-level
    # import — including `birdbrain web`, which never runs inference. Only this
    # one actually needs it.
    from birdbrain.pipeline import run_all

    run_all(sources, cfg)


@app.command(name="node-sync")
def node_sync(
    config_file: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to this node's node.toml."),
    ] = Path("node.toml"),
    sources_file: Annotated[
        Path | None,
        typer.Option("--sources", "-s", help="Path to a sources.toml file."),
    ] = None,
    once: Annotated[
        bool,
        typer.Option("--once", help="Run a single pass and exit (for a cron/smoke test)."),
    ] = False,
) -> None:
    """Report this ingest node's detections to a central birdbrain.

    Runs beside `birdbrain run` on a node whose sources.toml holds cams that
    central itself does not analyse. Each link in node.toml maps one local
    source to one device enrolled on central, so every cam arrives as its own
    site at its own coordinates. Central needs no changes to accept them — see
    birdbrain.node_sync.
    """
    # Imported here for the same reason `run` defers the pipeline: this pulls in
    # requests and a socket stack that no other subcommand touches, and only
    # a node ever runs this one.
    from birdbrain.node_sync import MarkStore, load_node_config, run_node_sync, sync_once

    cfg = AppConfig()
    if sources_file is not None:
        cfg.sources_file = sources_file
    configure_logging(cfg.log_level)
    node_cfg = load_node_config(config_file)
    if not node_cfg.links:
        console.print(f"[red]No [[link]] entries in {config_file}.[/red]")
        raise typer.Exit(code=1)

    # Fail on a typo'd source name here rather than syncing an empty backlog
    # forever: a link whose source matches nothing in sources.toml always finds
    # zero rows, which is indistinguishable from a quiet cam in the logs.
    sources = load_sources(cfg.sources_file)
    known = {s.name for s in sources}
    unknown = [ln.source for ln in node_cfg.links if ln.source not in known]
    if unknown:
        console.print(
            f"[red]node.toml links reference sources not in {cfg.sources_file}:[/red] "
            + ", ".join(repr(u) for u in unknown)
        )
        raise typer.Exit(code=1)
    timezones = {s.name: s.timezone for s in sources}

    db = Database(cfg.db_url)
    if once:
        marks = MarkStore.load(node_cfg.state_file)
        results = sync_once(db, node_cfg, marks, timezones)
        for name, sent in sorted(results.items()):
            console.print(f"  {name}: {sent} sent")
        console.print(f"[green]Synced {sum(results.values())} detections.[/green]")
        return

    stop = threading.Event()
    try:
        run_node_sync(db, node_cfg, stop, timezones)
    except KeyboardInterrupt:
        stop.set()
        console.print("Stopped.")


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


@app.command(name="backfill-briefs")
def backfill_briefs(
    start: Annotated[
        str, typer.Option("--from", help="First UTC date (YYYY-MM-DD), inclusive.")
    ],
    end: Annotated[
        str | None,
        typer.Option("--to", help="Last UTC date, inclusive. Default: yesterday (UTC)."),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Regenerate dates that already have a brief."),
    ] = False,
) -> None:
    """Generate daily briefs for a past UTC date range — fills gaps left by an
    outage (e.g. the notes worker stalled on exhausted API credits). Skips dates
    that already have a brief unless --overwrite, and skips dates with no data."""
    from datetime import date as _date

    from birdbrain.notes import generate_brief_for_date

    cfg = AppConfig()
    configure_logging(cfg.log_level)
    db = Database(cfg.db_url)
    sources = load_sources(cfg.sources_file)

    d0 = _date.fromisoformat(start)
    d1 = _date.fromisoformat(end) if end else (datetime.now(UTC).date() - timedelta(days=1))
    if d1 < d0:
        console.print("[red]--to is before --from[/red]")
        raise typer.Exit(code=1)

    have = {b.date_utc for b in db.list_daily_briefs(limit=400)}
    d = d0
    made = skipped = empty = failed = 0
    while d <= d1:
        if d in have and not overwrite:
            console.print(f"[dim]{d} — already have a brief, skipping[/dim]")
            skipped += 1
        else:
            try:
                text = generate_brief_for_date(db, cfg, d, sources)
                if text:
                    console.print(f"[green]{d} — generated ({len(text)} chars)[/green]")
                    made += 1
                else:
                    console.print(f"[yellow]{d} — no detections that day, skipped[/yellow]")
                    empty += 1
            except Exception as e:  # noqa: BLE001 — report and continue the range
                console.print(f"[red]{d} — failed: {str(e)[:200]}[/red]")
                failed += 1
        d += timedelta(days=1)

    console.print(
        f"\nDone: [green]{made} generated[/green], {skipped} skipped, "
        f"{empty} no-data, [red]{failed} failed[/red]."
    )
    if failed:
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
    from birdbrain.detector import BirdNetDetector
    from birdbrain.pipeline import build_source

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


@app.command(name="tbb-listen")
def tbb_listen(
    device: Annotated[
        str,
        typer.Option("--device", "-d", help="ALSA capture device of the USB mic."),
    ] = "plughw:1,0",
    seconds: Annotated[
        int, typer.Option("--seconds", "-t", help="How long to listen before stopping.")
    ] = 60,
    min_confidence: Annotated[
        float, typer.Option("--min-confidence", help="BirdNET confidence floor.")
    ] = 0.5,
    lat: Annotated[
        float | None, typer.Option("--lat", help="Latitude for the locality filter.")
    ] = None,
    lon: Annotated[
        float | None, typer.Option("--lon", help="Longitude for the locality filter.")
    ] = None,
) -> None:
    """TBB bench smoke test: capture from a USB mic and run BirdNET per chunk.

    Builds a :class:`MicSource`, streams 3 s chunks off the ALSA device, runs the
    detector on each, and prints detections plus the measured per-chunk inference
    time. Use this on a Raspberry Pi Zero 2 W to validate the real-time margin;
    wrap it in ``/usr/bin/time -v`` (or watch ``free -m``) to capture peak RAM.
    """
    import time

    from birdbrain.audio import MicSource
    from birdbrain.detector import BirdNetDetector

    cfg = AppConfig()
    configure_logging(cfg.log_level)

    source = MicSource(
        name="tbb-bench",
        device=device,
        sample_rate=cfg.sample_rate,
        chunk_seconds=cfg.chunk_seconds,
    )

    console.rule(f"[bold]tbb-listen[/bold]  device={device}  {seconds}s")
    t_load = time.perf_counter()
    detector = BirdNetDetector()
    console.print(f"detector loaded in {time.perf_counter() - t_load:.1f}s")

    deadline = time.perf_counter() + seconds
    inference_ms: list[float] = []
    n_chunks = 0
    n_dets = 0
    stream = source.stream()
    for chunk in stream:
        n_chunks += 1
        t0 = time.perf_counter()
        dets = detector.analyze(chunk, lat=lat, lon=lon, min_confidence=min_confidence)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        inference_ms.append(dt_ms)
        n_dets += len(dets)
        margin = chunk.duration_s * 1000.0 / dt_ms if dt_ms else float("inf")
        console.print(
            f"chunk {n_chunks}: {dt_ms:7.1f} ms  "
            f"(x{margin:.1f} real-time)  {len(dets)} detection(s)"
        )
        for d in dets:
            console.print(
                f"  {d.common_name} ({d.scientific_name})  conf={d.confidence:.2f}"
            )
        if time.perf_counter() >= deadline:
            break

    console.rule("[bold]summary[/bold]")
    if inference_ms:
        avg = sum(inference_ms) / len(inference_ms)
        console.print(
            f"chunks={n_chunks}  detections={n_dets}\n"
            f"inference ms  min={min(inference_ms):.1f}  "
            f"avg={avg:.1f}  max={max(inference_ms):.1f}\n"
            f"chunk budget={cfg.chunk_seconds * 1000.0:.0f} ms "
            f"→ headroom x{cfg.chunk_seconds * 1000.0 / avg:.1f} at the mean"
        )
    else:
        console.print("[red]no chunks captured — check the ALSA device with `arecord -l`[/red]")

    try:
        import resource  # Unix only; gives peak RSS for the inline RAM datapoint.

        peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        console.print(f"peak RSS (this process): {peak_kb / 1024:.0f} MB")
    except ImportError:
        console.print("[dim]peak RSS unavailable here; use `/usr/bin/time -v` on the Pi.[/dim]")


@app.command(name="tbb-pipeline")
def tbb_pipeline() -> None:
    """Run the TBB capture-unit pipeline: USB mic → BirdNET → local SQLite + clips.

    Single mic source, no central-only workers, with a local clip-retention
    sweep. Configure via BIRDBRAIN_TBB_* env / the unit's .env (unit id, mic
    device, lat/lon, retention). This is the `tbb-pipeline` systemd service.
    """
    from birdbrain.tbb import run_tbb

    cfg = AppConfig()
    configure_logging(cfg.log_level)
    run_tbb(cfg)


@app.command(name="tbb-web")
def tbb_web(
    host: Annotated[
        str, typer.Option("--host", "-H", help="Bind address. 0.0.0.0 = reachable on the LAN.")
    ] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port", "-p")] = 8080,
) -> None:
    """Run the minimal LAN-only TBB unit web UI (Now / Today / Setup).

    Binds the LAN by default so a phone on the same wifi can reach it at
    http://<unit>.local:8080. The unit exposes no inbound ports to the
    internet — keep it off any port-forward (see tbb-architecture.md §8).
    """
    import uvicorn

    cfg = AppConfig()
    configure_logging(cfg.log_level)
    uvicorn.run(
        "birdbrain.web.tbb_app:app",
        host=host,
        port=port,
        log_level=cfg.log_level.lower(),
        # No per-request access log. The Now page polls /feed every 5s, so one
        # phone left open on a windowsill wrote ~17k lines a day into a
        # persistent journal on the SD card — to record that a page someone is
        # looking at is still being looked at. Errors still log; central keeps
        # its access log (cli.py's `web` command is untouched).
        access_log=False,
    )


@app.command(name="tbb-device-add")
def tbb_device_add(
    unit_id: Annotated[str, typer.Option("--unit-id", help="Unit id == its source_name on central.")],
    owner: Annotated[str | None, typer.Option("--owner")] = None,
    lat: Annotated[float | None, typer.Option("--lat")] = None,
    lon: Annotated[float | None, typer.Option("--lon")] = None,
    public: Annotated[
        bool, typer.Option("--public/--private", help="Show this unit on the public map.")
    ] = False,
) -> None:
    """Register a TBB unit on CENTRAL and print its bearer token (shown once).

    Run on the central server. Re-running rotates the token. Until the Phase 3
    /enroll flow exists, this is how a unit gets its sync credentials.
    """
    import secrets

    from birdbrain.ingest import hash_token

    cfg = AppConfig()
    db = Database(cfg.db_url)
    token = secrets.token_urlsafe(32)
    db.upsert_device(
        unit_id, hash_token(token), owner=owner, lat=lat, lon=lon,
        sync_enabled=True, public=public,
    )
    console.print(f"Registered device [bold]{unit_id}[/bold] (public={public}).")
    console.print("Device token — store it now, it is not recoverable:")
    console.print(f"  [green]{token}[/green]")
    console.print(
        "\nOn the unit's .env:\n"
        f"  BIRDBRAIN_TBB_DEVICE_TOKEN={token}\n"
        "  BIRDBRAIN_TBB_CENTRAL_URL=https://birdbrain.co.za\n"
        "  BIRDBRAIN_TBB_SYNC_ENABLED=true"
    )


@app.command(name="tbb-claim-new")
def tbb_claim_new(
    note: Annotated[
        str | None, typer.Option("--note", help="Reminder of which box/owner this code is for.")
    ] = None,
) -> None:
    """Generate a one-time enrollment claim code on CENTRAL (print it on the box).

    A unit redeems it from its /setup page; central then issues the unit's id +
    token automatically. Run on the central server.
    """
    import secrets

    cfg = AppConfig()
    db = Database(cfg.db_url)
    # Grouped 10 hex chars — short enough to type, long enough vs the /enroll
    # rate limit. Uppercased for legibility on a printed label.
    raw = secrets.token_hex(5).upper()
    code = f"{raw[:5]}-{raw[5:]}"
    db.create_claim_code(code, note=note)
    console.print("Claim code (print on the unit's box):")
    console.print(f"  [green]{code}[/green]")
    if note:
        console.print(f"  note: {note}")


@app.command(name="tbb-device-revoke")
def tbb_device_revoke(
    unit_id: Annotated[str, typer.Option("--unit-id", help="Unit to cut off from sync.")],
    enable: Annotated[
        bool, typer.Option("--enable/--revoke", help="Re-enable instead of revoking.")
    ] = False,
) -> None:
    """Revoke (or re-enable) a unit's sync on CENTRAL. Revoking makes its token
    stop authorising ingest immediately; the unit's existing data is kept."""
    cfg = AppConfig()
    db = Database(cfg.db_url)
    if not db.set_device_sync(unit_id, enable):
        console.print(f"[red]No such device: {unit_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"{'Enabled' if enable else 'Revoked'} sync for [bold]{unit_id}[/bold].")


@app.command(name="seed-runtime")
def seed_runtime(
    sources_file: Annotated[
        Path | None,
        typer.Option("--sources", "-s", help="sources.toml to copy from."),
    ] = None,
) -> None:
    """Copy sources.toml entries into the runtime_sources table.

    File-managed sources can only be reconfigured by editing sources.toml +
    restarting the pipeline. Runtime sources can be stopped, restarted,
    and have their min_confidence edited from /admin. This command promotes
    each toml entry to a runtime row so the UI can govern it.

    Idempotent — running twice updates the existing row with the toml's
    current values without duplicating.
    """
    cfg = AppConfig()
    if sources_file is not None:
        cfg.sources_file = sources_file
    configure_logging(cfg.log_level)
    sources = load_sources(cfg.sources_file)
    if not sources:
        console.print("[yellow]sources.toml is empty or missing — nothing to seed.[/yellow]")
        raise typer.Exit(code=0)
    db = Database(cfg.db_url)
    for s_cfg in sources:
        db.add_runtime_source(
            name=s_cfg.name,
            kind=s_cfg.kind,
            url=s_cfg.url,
            lat=s_cfg.lat,
            lon=s_cfg.lon,
            min_confidence=s_cfg.min_confidence,
            multisite=s_cfg.multisite,
            cookies_from_browser=s_cfg.cookies_from_browser,
            cookies_file=str(s_cfg.cookies_file) if s_cfg.cookies_file else None,
            timezone=s_cfg.timezone,
        )
        console.print(f"  ✓ {s_cfg.name}")
    console.print(
        f"\nSeeded {len(sources)} runtime sources. "
        "Restart the pipeline so the supervisor reattaches under the new rows."
    )


@app.command()
def prune(
    days: Annotated[int, typer.Option(
        "--days", "-d",
        help="Global clip window in days. Per-species overrides (see clip-retention) shorten it.",
    )] = 30,
    keep_audited: Annotated[bool, typer.Option(
        "--keep-audited/--no-keep-audited",
        help="Keep every clip a person labelled, rated or corrected.",
    )] = True,
    keep_reference: Annotated[bool, typer.Option(
        "--keep-reference/--no-keep-reference",
        help="Keep the newest low/mid/high-confidence clip of every species at every source.",
    )] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Count and report; delete nothing.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the y/N confirmation prompt.")] = False,
) -> None:
    """Delete expired detection clips to free disk. DB rows are kept; clip_path
    is NULLed for any row whose audio file gets removed. See
    birdbrain.retention for the rules — age window, per-species overrides,
    audited clips kept, and a low/mid/high reference set per species and
    source that never expires."""
    cfg = AppConfig()
    db = Database(cfg.db_url)
    policy = RetentionPolicy(
        days=days,
        species_days=db.species_clip_retention_map(),
        keep_audited=keep_audited,
        keep_reference_set=keep_reference,
    )

    def _report(stats, title: str) -> None:
        t = Table(title=title)
        t.add_column("metric")
        t.add_column("value", justify="right")
        t.add_row("days scanned", f"{stats.days_scanned:,}")
        t.add_row("expired rows", f"{stats.rows_seen:,}")
        t.add_row("rows kept (file shared with a protected row)", f"{stats.rows_kept_shared_file:,}")
        t.add_row("clip files", f"{stats.files_deleted:,}")
        t.add_row("cached mp3/png siblings", f"{stats.siblings_deleted:,}")
        t.add_row("missing on disk", f"{stats.files_missing:,}")
        t.add_row("rows NULLed", f"{stats.rows_nulled:,}")
        t.add_row("freed", f"{stats.mb_freed:,.1f} MB")
        t.add_row("protected files (audited + reference set)", f"{stats.protected_files:,}")
        t.add_row("species overrides", f"{len(policy.species_days):,}")
        console.print(t)
        if stats.by_source:
            per_src = Table(title="By source")
            per_src.add_column("source")
            per_src.add_column("rows", justify="right")
            for src in sorted(stats.by_source):
                per_src.add_row(src, f"{stats.by_source[src]:,}")
            console.print(per_src)

    if dry_run or not yes:
        plan = prune_clips(db, policy, dry_run=True)
        _report(plan, f"Prune plan (older than {days}d)")
        if dry_run:
            return
        if not plan.files_deleted and not plan.rows_nulled:
            console.print("[green]nothing to prune.[/green]")
            return
        ans = typer.prompt("delete? [y/N]", default="N", show_default=False)
        if ans.strip().lower() not in {"y", "yes"}:
            console.print("[yellow]aborted.[/yellow]")
            raise typer.Exit(code=1)

    def _progress(day, stats) -> None:
        if stats.by_source:
            console.print(
                f"  {day}  files={stats.files_deleted:,}  rows={stats.rows_nulled:,}  "
                f"freed={stats.mb_freed:,.0f} MB",
                highlight=False,
            )

    stats = prune_clips(db, policy, on_day=_progress)
    console.print(
        f"[green]deleted {stats.files_deleted:,} clip files[/green] "
        f"(+{stats.siblings_deleted:,} cached siblings, {stats.mb_freed:,.1f} MB), "
        f"NULLed clip_path on {stats.rows_nulled:,} rows."
    )


@app.command("clip-retention")
def clip_retention(
    species: Annotated[str | None, typer.Argument(
        help="Scientific or common name, e.g. 'Alopochen aegyptiaca' or 'Egyptian Goose'. "
             "Omit to list current overrides.",
    )] = None,
    days: Annotated[int | None, typer.Option(
        "--days", help="Keep this species' clips this many days instead of the global window.",
    )] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove the override.")] = False,
) -> None:
    """Per-species clip retention overrides, applied by `birdbrain prune`.
    Give loud, unmistakable species (Egyptian Goose, Hadada Ibis, Grey
    Go-away-bird) a short window; the reference set and audited clips are kept
    regardless."""
    cfg = AppConfig()
    db = Database(cfg.db_url)
    if species is None:
        overrides = db.species_clip_retention_map()
        if not overrides:
            console.print("no per-species overrides; the global window applies to everything.")
            return
        names = _common_names(db, overrides)
        t = Table(title="Clip retention overrides")
        t.add_column("species")
        t.add_column("scientific")
        t.add_column("days", justify="right")
        for sci in sorted(overrides, key=lambda k: names.get(k, k)):
            t.add_row(names.get(sci, ""), sci, str(overrides[sci]))
        console.print(t)
        return
    sci = _resolve_species(db, species)
    if sci is None:
        console.print(f"[red]no detections match {species!r}[/red] (try the scientific name)")
        raise typer.Exit(code=1)
    if clear:
        db.set_species_clip_retention_days(sci, None)
        console.print(f"cleared override for {sci}; the global window applies.")
        return
    if days is None:
        console.print("[red]--days N or --clear is required[/red]")
        raise typer.Exit(code=2)
    db.set_species_clip_retention_days(sci, days)
    console.print(f"{sci}: clips kept {days} day(s).")


def _resolve_species(db: Database, name: str) -> str | None:
    """Scientific name for ``name``, accepting a common name (case-insensitive)."""
    with db.session() as s:
        hit = s.scalar(
            select(DetectionRow.scientific_name)
            .where(DetectionRow.scientific_name == name)
            .limit(1)
        )
        if hit:
            return hit
        return s.scalar(
            select(DetectionRow.scientific_name)
            .where(func.lower(DetectionRow.common_name) == name.strip().lower())
            .limit(1)
        )


def _common_names(db: Database, scientific: Iterable[str]) -> dict[str, str]:
    scis = list(scientific)
    if not scis:
        return {}
    with db.session() as s:
        rows = s.execute(
            select(DetectionRow.scientific_name, DetectionRow.common_name)
            .where(DetectionRow.scientific_name.in_(scis))
            .distinct()
        )
        return {sci: common for sci, common in rows}


@app.command()
def web(
    host: Annotated[str, typer.Option("--host", "-H")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p")] = 8000,
    reload: Annotated[bool, typer.Option("--reload")] = False,
    workers: Annotated[int, typer.Option("--workers", "-w")] = 1,
) -> None:
    """Run the web dashboard. Reads from the same SQLite as the pipeline."""
    import uvicorn

    cfg = AppConfig()
    configure_logging(cfg.log_level)
    uvicorn.run(
        "birdbrain.web.app:app",
        host=host,
        port=port,
        reload=reload,
        # >1 spins up a multiprocess supervisor. SQLite is WAL + busy_timeout so
        # concurrent readers/writers are safe; background singletons (media
        # sweeper) are flock-gated to one worker. reload and workers>1 are
        # mutually exclusive, so only pass workers when actually scaling out.
        workers=workers if workers > 1 else None,
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
    ``birdbrain.audio_hash.clip_hash``. Idempotent — rerunnable, will only
    touch rows that still lack a hash.
    """
    from birdbrain.audio_hash import clip_hash

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
    from birdbrain.web import auth as auth_mod

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


@app.command(name="set-role")
def set_role(
    username: Annotated[str, typer.Argument(help="Existing account username.")],
    role: Annotated[
        str, typer.Option("--role", help="Role to assign, e.g. operator or tester.")
    ],
) -> None:
    """Set an account's role.

    ``operator`` is the admin role: it is what lets an account reach /admin
    (and, when enabled, /admin/terminal) over the public tunnel. Granting it is
    granting a shell — the terminal runs as the web service's own user. Pair it
    with an edge authenticator; see docs/remote-admin.md.
    """
    from birdbrain.web import auth as auth_mod

    cfg = AppConfig()
    db = Database(cfg.db_url)
    uname = auth_mod.normalize_username(username)
    if not db.set_user_role(uname, role):
        console.print(f"[red]No such user: {uname!r}.[/red]")
        raise typer.Exit(1)
    console.print(f"[green]{uname!r} is now {role!r}.[/green]")
    if role in auth_mod.ADMIN_ROLES:
        console.print(
            "[yellow]That is an admin role — this account can now reach /admin "
            "remotely, and a shell if BIRDBRAIN_TERMINAL_ENABLED is on.[/yellow]"
        )


if __name__ == "__main__":
    app()


@app.command(name="build-confusions")
def build_confusions(
    per_species: Annotated[
        int,
        typer.Option("--per-species", help="Clips to sample per species."),
    ] = 25,
    min_detections: Annotated[
        int,
        typer.Option("--min-detections",
                     help="Skip species with fewer clips than this."),
    ] = 5,
    species: Annotated[
        str | None,
        typer.Option("--species", help="Scientific name; default is every species."),
    ] = None,
    max_clips: Annotated[
        int,
        typer.Option("--max-clips", help="Stop after this many clips overall."),
    ] = 2000,
) -> None:
    """Build the empirical confusion matrix by re-running BirdNET on stored clips.

    For each clip it records everything BirdNET proposes besides the species
    the clip is filed as. Enough of those and you have measured what this model
    actually mistakes for what, in our audio, at our cameras -- which is a
    different and better answer than "what is related to it" or "what has
    somebody written that it resembles".

    Resumable and idempotent: clips already analysed are skipped, so this can
    be run in short bursts and interrupted freely. The review page's re-analyze
    panel feeds the same table, so the matrix also improves on its own as
    people use it.

    Highest-confidence clips are sampled first -- a matrix built from the ones
    the model was least sure about would describe its worst moments rather than
    its habitual mistakes.
    """
    import librosa
    import numpy as np

    from birdbrain.audio.source import AudioChunk
    from birdbrain.detector.birdnet import BirdNetDetector

    cfg = AppConfig()
    configure_logging(cfg.log_level)
    db = Database(cfg.db_url)

    if species:
        targets = [(species, per_species)]
    else:
        targets = [
            (sci, per_species)
            for sci, _common, _n, clips in db.species_catalog()
            if clips >= min_detections
        ]
    if not targets:
        console.print("[yellow]no species with enough clips[/]")
        return

    detector = BirdNetDetector()
    done = skipped = failed = 0
    console.print(f"{len(targets)} species, up to {per_species} clips each")

    for sci, want in targets:
        if done >= max_clips:
            break
        for det_id in db.clips_needing_reanalysis(sci, want):
            if done >= max_clips:
                break
            with db.session() as s:
                row = s.get(DetectionRow, det_id)
                if row is None or not row.clip_path:
                    continue
                wav_path = Path(row.clip_path)
                lat, lon = row.latitude, row.longitude
                started = row.started_at
                source_name = row.source_name
            if not wav_path.is_file():
                # Retention pruned the audio out from under the row.
                skipped += 1
                continue
            try:
                samples, sr = librosa.load(str(wav_path), sr=48_000, mono=True)
                chunk = AudioChunk(
                    samples=np.asarray(samples, dtype=np.float32),
                    sample_rate=int(sr),
                    started_at=started or datetime.now(UTC),
                    source_name=source_name,
                )
                # Same low floor the review panel uses, so both paths bank
                # comparable observations into one table.
                found = detector.analyze(
                    chunk, lat=lat, lon=lon, week=None, min_confidence=0.05
                )
            except Exception as e:
                failed += 1
                console.print(f"[red]{det_id}: {e}[/]")
                continue
            db.record_reanalysis(
                det_id, sci,
                [(d.scientific_name, d.confidence) for d in found
                 if d.scientific_name != sci],
            )
            done += 1
            if done % 50 == 0:
                console.print(f"  {done} clips…")

    edges = db.confusion_edges(min_clips=2)
    console.print(
        f"[green]analysed {done}[/] clips "
        f"({skipped} missing audio, {failed} failed) — "
        f"{len(edges)} confusion pairs at >=2 clips"
    )


def _pick_floor(
    own: list[float],
    wrong: list[tuple[float, int]],
    baseline: float,
    keep_own: float,
    suppress: float,
) -> tuple[float, float, float] | None:
    """The smallest floor above ``baseline`` that keeps ``keep_own`` of a
    species' own detections while removing ``suppress`` of its wrong firings,
    or None when no such floor exists.

    Strictly above the baseline, never at or below it. The baseline is the
    floor already in force, so a "proposal" beneath it would be a proposal to
    record *more* of what we are trying to suppress -- which is what came back
    the first time this was measured against re-analysis' 0.05 rather than
    against what capture actually keeps.

    Returns (floor, fraction of own kept, fraction of wrong removed).
    """
    if not own or not wrong:
        return None
    obs = sum(n for _c, n in wrong)
    if obs <= 0:
        return None
    for step in range(int(baseline / 0.05) + 1, 20):
        f = round(step * 0.05, 2)
        kept = sum(1 for x in own if x >= f) / len(own)
        gone = sum(n for c, n in wrong if c < f) / obs
        if kept >= keep_own and gone >= suppress:
            return (f, kept, gone)
    return None


@app.command(name="suggest-floors")
def suggest_floors(
    apply_: Annotated[
        bool,
        typer.Option("--apply", help="Write the floors. Without this, only prints them."),
    ] = False,
    keep_own: Annotated[
        float,
        typer.Option("--keep-own",
                     help="Fraction of a species' own detections a floor must keep."),
    ] = 0.95,
    suppress: Annotated[
        float,
        typer.Option("--suppress",
                     help="Fraction of its wrong firings a floor must remove."),
    ] = 0.70,
    min_obs: Annotated[
        int,
        typer.Option("--min-obs", help="Wrong firings required before a species is considered."),
    ] = 15,
    min_genera: Annotated[
        int,
        typer.Option("--min-genera", help="Distinct genera it must fire across."),
    ] = 5,
) -> None:
    """Propose per-species confidence floors for false-positive attractors.

    An attractor is a label BirdNET reaches for when it is unsure: it turns up
    on many other species' clips, across many genera, rather than being
    confused with one particular bird. Egyptian Goose fires on 54 genera at a
    mean of 0.42 while its own detections sit at 0.95 and up -- so a floor
    around 0.9 costs almost nothing and removes the lot.

    A floor is only proposed where that gap exists: it must remove most of the
    species' wrong firings while keeping nearly all of its own detections. Many
    attractors have no such floor, because they fire wrongly at the same
    confidences they fire rightly, and those are reported and left alone.

    Prints the trade-off and changes nothing unless --apply is given. The
    floors take effect at capture (pipeline and ingest), so they suppress
    future detections rather than hiding existing ones.

    Reads the confusion matrix, so run `build-confusions` first.
    """
    cfg = AppConfig()
    configure_logging(cfg.log_level)
    db = Database(cfg.db_url)

    def genus(s: str) -> str:
        return s.partition(" ")[0]

    # Wrong firings, approximated at pair granularity: each pair contributes
    # its clip count at its mean confidence. We store sums rather than every
    # observation, so this is a histogram of pair means, not of firings.
    wrong: dict[str, list[tuple[float, int]]] = {}
    spread: dict[str, set[str]] = {}
    for subj, other, clips, mean_conf in db.confusion_edges(min_clips=1):
        if genus(subj) == genus(other):
            continue          # congener overlap is real ambiguity, not an attractor
        wrong.setdefault(other, []).append((mean_conf, clips))
        spread.setdefault(other, set()).add(genus(subj))

    own_conf = db.confidences_by_species()
    existing = db.species_min_confidence_map()
    names = {sci: common for sci, common, _n, _c in db.species_catalog()}

    # Re-analysis runs at 0.05 so runners-up surface; capture never did. A
    # species' own lowest recorded confidence is therefore the exact floor its
    # sources were applying, override included — nothing below it was ever
    # going to become a detection. Counting those firings would propose floors
    # that suppress nothing, and for a species already floored, propose
    # lowering it: Egyptian Goose sits at 0.90 and, measured against 0.05,
    # looked like it wanted 0.55.

    proposals: list[tuple] = []
    no_gap: list[tuple] = []
    for sci, hits in wrong.items():
        if len(spread.get(sci, ())) < min_genera:
            continue
        own_all = own_conf.get(sci) or []
        if len(own_all) < 50:
            continue
        baseline = max(existing.get(sci) or 0.0, min(own_all))
        # Only firings that would actually have been recorded.
        live = [(c, n) for c, n in hits if c >= baseline]
        obs = sum(n for _c, n in live)
        if obs < min_obs:
            continue
        own = sorted(own_all)
        chosen = _pick_floor(own, live, baseline, keep_own, suppress)
        row = (sci, names.get(sci, sci), obs, len(spread[sci]), len(own))
        if chosen:
            proposals.append((*row, *chosen))
        else:
            no_gap.append(row)

    proposals.sort(key=lambda r: -r[2])
    console.print(f"\n[bold]{len(proposals)} floors proposed[/] "
                  f"({len(no_gap)} attractors have no safe floor)\n")
    console.print(f"  {'species':30s} {'wrong':>6s} {'gen':>4s} {'floor':>6s} "
                  f"{'own kept':>9s} {'wrong gone':>11s} {'current':>8s}")
    for sci, common, obs, gen, _n, f, kept, gone in proposals:
        cur = existing.get(sci)
        console.print(f"  {common[:30]:30s} {obs:6d} {gen:4d} {f:6.2f} "
                      f"{kept*100:8.1f}% {gone*100:10.1f}% "
                      f"{(f'{cur:.2f}' if cur is not None else '—'):>8s}")

    if no_gap:
        console.print(f"\n[yellow]no safe floor[/] (fires wrongly at the "
                      f"confidences it fires rightly): "
                      f"{', '.join(n for _s, n, *_ in no_gap[:8])}"
                      f"{' …' if len(no_gap) > 8 else ''}")

    if not apply_:
        console.print("\n[dim]nothing written — pass --apply to set these[/]")
        return
    for sci, _common, _o, _g, _n, f, _k, _s in proposals:
        db.set_species_min_confidence(sci, f)
    console.print(f"\n[green]applied {len(proposals)} floors[/] — "
                  f"they take effect at capture within a few minutes")
