from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import desc, select

from africam.config import AppConfig, load_sources
from africam.logging import configure as configure_logging
from africam.pipeline import run_all
from africam.storage import Database, DetectionRow

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


if __name__ == "__main__":
    app()
