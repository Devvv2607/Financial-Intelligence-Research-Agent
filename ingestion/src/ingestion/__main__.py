"""CLI: `python -m ingestion run|report`.

python -m ingestion run --universe data/universe.csv [--sample 10] [--tickers A,B]
                        [--only edgar|tagging|news|index] [--force] [--refresh]
                        [--experiment-headings-only]
python -m ingestion report [--sample 10 | --tickers A,B] [--section sectors]
"""

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from sqlalchemy import select

from core import aio
from core.config import Settings, get_settings
from core.db import create_engine
from core.db.llm_store import PostgresLLMStore
from core.db.schema import companies
from core.embeddings import Embedder
from core.llm import GroqClient
from core.logging import configure_logging
from core.vectorstore import create_client, ensure_ready
from ingestion import report as rpt
from ingestion.edgar.client import EdgarClient
from ingestion.news.client import NewsApiClient
from ingestion.pipeline import SAMPLE_TICKERS, STAGES, Pipeline, Stage, UniverseRow, read_universe

console = Console()


def _select_rows(rows: list[UniverseRow], args: argparse.Namespace) -> list[UniverseRow]:
    if args.tickers:
        wanted = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif args.sample:
        wanted = list(SAMPLE_TICKERS[: args.sample])
    else:
        return rows
    by_ticker = {r.ticker: r for r in rows}
    missing = [t for t in wanted if t not in by_ticker]
    if missing:
        sys.exit(f"not in universe: {', '.join(missing)}")
    return [by_ticker[t] for t in wanted]


async def _run(args: argparse.Namespace, s: Settings) -> int:
    rows = _select_rows(read_universe(Path(args.universe)), args)
    stages: tuple[Stage, ...] = (args.only,) if args.only else STAGES
    engine = create_engine(s.database_url)
    edgar = EdgarClient(s.sec_user_agent, s.raw_dir, s.sec_requests_per_second)
    llm = GroqClient(s, PostgresLLMStore(engine))
    news = (
        NewsApiClient(s.newsapi_key.get_secret_value(), s.newsapi_base_url, s.raw_dir)
        if s.newsapi_key
        else None
    )
    wv = create_client(s)
    await ensure_ready(wv)
    console.print(f"Loading embedding model {s.embedding_model}…")
    embedder = Embedder(s.embedding_model, s.models_dir)

    started = datetime.now(UTC)
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("companies", total=len(rows))

        def on_progress(ticker: str, msg: str) -> None:
            if msg == "finished":
                progress.advance(task)
            else:
                progress.update(task, description=f"{ticker}: {msg}")

        pipeline = Pipeline(
            s,
            engine,
            edgar,
            llm,
            embedder,
            wv,
            news,
            experiment_headings_only=args.experiment_headings_only,
            on_progress=on_progress,
        )
        outcomes = await pipeline.run(rows, stages, force=args.force, refresh=args.refresh)

    ok = [o for o in outcomes if all(o.stages.get(st) == "done" for st in stages if st != "news")]
    failed = [o for o in outcomes if o not in ok]
    console.print(
        f"\n[bold]{len(ok)}/{len(outcomes)} companies OK[/] in "
        f"{(datetime.now(UTC) - started).total_seconds() / 60:.1f} min · "
        f"SEC network requests: {edgar.network_requests} · "
        f"NewsAPI requests: {news.network_requests if news else 0}"
    )
    for o in failed:
        console.print(
            f"  [red]{o.ticker}[/]: "
            + "; ".join(f"{k}={o.stages.get(k)} ({v})" for k, v in o.errors.items())
        )
    for client in (edgar, llm, news):
        if client is not None:
            await client.aclose()
    await wv.close()
    await engine.dispose()
    return 0 if not failed else 1


async def _report(args: argparse.Namespace, s: Settings) -> int:
    universe = read_universe(Path(args.universe))
    rows = _select_rows(universe, args)
    engine = create_engine(s.database_url)
    async with engine.connect() as conn:
        ciks = list(
            (
                await conn.execute(
                    select(companies.c.cik).where(companies.c.ticker.in_([r.ticker for r in rows]))
                )
            ).scalars()
        )
    if args.section == "sectors":
        text = await rpt.sector_section(engine, ciks)
    else:
        report = await rpt.build_report(engine, s, ciks, len(universe))
        text = report.markdown()
    await engine.dispose()
    s.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = s.reports_dir / f"ingest-{args.section}-{stamp}.md"
    path.write_text(f"# Ingestion report ({stamp})\n\n{text}\n", encoding="utf-8")
    console.print(Markdown(text))
    console.print(f"\nSaved to {path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ingestion")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "report"):
        p = sub.add_parser(name)
        p.add_argument("--universe", default="data/universe.csv")
        p.add_argument("--sample", type=int, help=f"first N of the agreed sample {SAMPLE_TICKERS}")
        p.add_argument("--tickers", help="comma-separated tickers")
    run = sub.choices["run"]
    run.add_argument("--only", choices=STAGES)
    run.add_argument("--force", action="store_true", help="re-run stages already done")
    run.add_argument("--refresh", action="store_true", help="re-download SEC submissions")
    run.add_argument(
        "--experiment-headings-only",
        action="store_true",
        help="also tag latest-year risks from headings only (for comparison)",
    )
    sub.choices["report"].add_argument("--section", choices=["all", "sectors"], default="all")
    args = parser.parse_args()

    s = get_settings()
    configure_logging("WARNING")  # progress bar + report are the interactive output
    logging.getLogger("ingestion").setLevel(s.log_level)
    handler = _run if args.command == "run" else _report
    sys.exit(aio.run(handler(args, s)))


if __name__ == "__main__":
    main()
