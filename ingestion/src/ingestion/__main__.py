"""CLI: `python -m ingestion run|report|experiment|review|progress`.

python -m ingestion run --universe data/universe.csv [--sample 10] [--tickers A,B]
                        [--only edgar|tagging|news|index] [--force] [--refresh]
                        [--daily] [--daily-budget N] [--batch-size N] [--mode M]
python -m ingestion report [--sample 10 | --tickers A,B] [--section sectors]
python -m ingestion experiment --tickers A,B --configs json_schema:12,json_schema:25
python -m ingestion review export|score [--path data/review/risk_tags_review.csv]
python -m ingestion progress
"""

import argparse
import logging
import random
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from core import aio
from core.config import Settings, get_settings
from core.db import create_engine
from core.db.llm_store import PostgresLLMStore
from core.db.schema import companies, risk_factors
from core.embeddings import Embedder
from core.llm import GroqClient, estimate_tokens
from core.logging import configure_logging
from core.newsapi import NewsApiClient
from core.vectorstore import create_client, ensure_ready
from ingestion import experiments, prompts, review
from ingestion import report as rpt
from ingestion.edgar.client import EdgarClient
from ingestion.enrich.tag_replay import load_cached_tag_plan, replay_cached_tags
from ingestion.load import postgres as pg
from ingestion.pipeline import SAMPLE_TICKERS, STAGES, Pipeline, Stage, UniverseRow, read_universe

console = Console()
REVIEW_SHEET = Path("data/review/risk_tags_review.csv")


def _select_rows(rows: list[UniverseRow], args: argparse.Namespace) -> list[UniverseRow]:
    if getattr(args, "tickers", None):
        wanted = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif getattr(args, "sample", None):
        wanted = list(SAMPLE_TICKERS[: args.sample])
    else:
        return rows
    by_ticker = {r.ticker: r for r in rows}
    missing = [t for t in wanted if t not in by_ticker]
    if missing:
        sys.exit(f"not in universe: {', '.join(missing)}")
    return [by_ticker[t] for t in wanted]


def _new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


async def _run(args: argparse.Namespace, s: Settings) -> int:
    universe = read_universe(Path(args.universe))
    rows = _select_rows(universe, args)
    stages: tuple[Stage, ...] = (args.only,) if args.only else STAGES
    engine = create_engine(s.database_url)
    if args.needs_review:
        async with engine.connect() as conn:
            flagged_tickers = set(
                (
                    await conn.execute(
                        select(companies.c.ticker)
                        .join(risk_factors, risk_factors.c.cik == companies.c.cik)
                        .where(risk_factors.c.needs_review.is_(True))
                        .distinct()
                    )
                ).scalars()
            )
        rows = [row for row in rows if row.ticker in flagged_tickers]
        if args.from_cache:
            cache_plan = await load_cached_tag_plan(engine)
            sample = random.SystemRandom().sample(
                cache_plan.samples, min(10, len(cache_plan.samples))
            )
            flagged_count = await _flagged_count(engine)
            console.print(
                f"cache rows to apply: {len(cache_plan.tags)}; "
                f"rows going to LLM: {flagged_count - len(cache_plan.tags)}"
            )
            for ticker, heading, category in sample:
                console.print(f"  {ticker} | {heading[:100]} | {category}")
            if args.dry_run:
                await _print_retag_plan(engine, s, {t.key for t in cache_plan.tags})
                await engine.dispose()
                return 0
            await pg.save_tags(
                engine,
                cache_plan.tags,
                f"{prompts.version('risk_tagging')}@{s.model_for('fast')}",
            )
        if args.dry_run:
            await _print_retag_plan(engine, s)
            await engine.dispose()
            return 0
    if args.daily:
        console.print("[bold]Before:[/] " + await rpt.progress_line(engine, s, len(universe)))
    run_id = _new_run_id()
    edgar = EdgarClient(s.sec_user_agent, s.raw_dir, s.sec_requests_per_second)
    llm = GroqClient(s, PostgresLLMStore(engine), run_id=run_id)
    news = (
        NewsApiClient(s.newsapi_key.get_secret_value(), s.newsapi_base_url, s.raw_dir)
        if s.newsapi_key
        else None
    )
    wv = create_client(s)
    await ensure_ready(wv)
    console.print(f"Run {run_id} | loading embedding model {s.embedding_model}…")
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
            run_id=run_id,
            concurrency=args.concurrency,
            experiment_tickers=frozenset(
                t.strip().upper() for t in (args.experiment_tickers or "").split(",") if t.strip()
            ),
            needs_review_only=args.needs_review,
            on_progress=on_progress,
        )
        outcomes = await pipeline.run(
            rows, stages, force=args.force or args.needs_review, refresh=args.refresh
        )

    failed = [o for o in outcomes if "failed" in o.stages.values()]
    paused = [o for o in outcomes if "paused" in o.stages.values() and o not in failed]
    ok = len(outcomes) - len(failed) - len(paused)
    console.print(
        f"\n[bold]{ok}/{len(outcomes)} companies complete[/], {len(paused)} paused at a daily "
        f"limit, {len(failed)} failed | "
        f"{(datetime.now(UTC) - started).total_seconds() / 60:.1f} min | "
        f"SEC requests: {edgar.network_requests} | NewsAPI requests: "
        f"{news.network_requests if news else 0}"
    )
    for o in failed:
        console.print(
            f"  [red]{o.ticker}[/]: "
            + "; ".join(f"{k}={o.stages.get(k)} ({v})" for k, v in o.errors.items())
        )
    if paused:
        reasons = sorted(
            {v for o in paused for k, v in o.errors.items() if o.stages[k] == "paused"}
        )
        console.print(f"  [yellow]paused[/]: {len(paused)} companies — " + "; ".join(reasons)[:300])
    if args.daily:
        console.print("[bold]After:[/] " + await rpt.progress_line(engine, s, len(universe)))
    for client in (edgar, llm, news):
        if client is not None:
            await client.aclose()
    await wv.close()
    await engine.dispose()
    return 1 if failed else 0  # pausing at a daily limit is the expected way a day ends


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
    _save(s, f"ingest-{args.section}", text)
    return 0


async def _experiment(args: argparse.Namespace, s: Settings) -> int:
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    configs = [experiments.Config.parse(c) for c in args.configs.split(",")]
    engine = create_engine(s.database_url)
    llm = GroqClient(s, PostgresLLMStore(engine))
    results = await experiments.run_tagging_experiment(
        engine, llm, s, tickers, configs, base_run_id=f"exp-{_new_run_id()}"
    )
    await llm.aclose()
    await engine.dispose()
    _save(s, "experiment-tagging", experiments.results_markdown(results, tickers))
    return 0


async def _review(args: argparse.Namespace, s: Settings) -> int:
    path = Path(args.path)
    if args.action == "export":
        engine = create_engine(s.database_url)
        version = f"{prompts.version('risk_tagging')}@{s.model_for('fast')}"
        n = await review.export_review_sheet(engine, path, version, n=args.n)
        await engine.dispose()
        console.print(
            f"Wrote {n} rows to {path}. Fill `your_category` with one of: "
            + ", ".join(review.CATEGORIES)
        )
        return 0
    # Score the exported (frozen) tags and today's stored tags on the same rows: after a
    # re-tag both numbers matter (e.g. prompt v2 at export vs v3 now).
    engine = create_engine(s.database_url)
    rows, _ = review.read_labels(path)
    sheet_version = args.sheet_version or "exported"
    compared = await review.score_sheet_and_current(engine, path, sheet_version)
    await engine.dispose()
    if not rows:
        console.print("No labelled rows yet: fill `your_category` first.")
        return 1
    _save(s, "review-score", review.compared_markdown(compared))
    return 0


async def _progress(args: argparse.Namespace, s: Settings) -> int:
    engine = create_engine(s.database_url)
    console.print(await rpt.progress_line(engine, s, len(read_universe(Path(args.universe)))))
    await engine.dispose()
    return 0


async def _replay_tag_cache(s: Settings) -> int:
    engine = create_engine(s.database_url)
    result = await replay_cached_tags(engine)
    await engine.dispose()
    console.print(
        f"Cached tag replay (read-only): {result['flagged_rows']} flagged rows, "
        f"{result['cached_echoes']} cached echoes, "
        f"{result['would_validate']} would validate, "
        f"{result['not_creditable']} not creditable"
    )
    return 0


async def _replay_handler(_args: argparse.Namespace, s: Settings) -> int:
    return await _replay_tag_cache(s)


async def _flagged_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(risk_factors)
                    .where(risk_factors.c.needs_review.is_(True))
                )
            ).scalar_one()
        )


async def _print_retag_plan(
    engine: AsyncEngine, s: Settings, excluded_ids: set[str] | None = None
) -> None:
    async with engine.connect() as conn:
        query = select(risk_factors.c.id, risk_factors.c.heading, risk_factors.c.body).where(
            risk_factors.c.needs_review.is_(True)
        )
        if excluded_ids:
            query = query.where(risk_factors.c.id.not_in(excluded_ids))
        rows = (
            await conn.execute(
                query.order_by(
                    risk_factors.c.cik, risk_factors.c.fiscal_year.desc(), risk_factors.c.ordinal
                )
            )
        ).all()
    count = len(rows)
    batches = (count + s.risk_tag_batch_size - 1) // s.risk_tag_batch_size
    estimated_tokens = sum(
        estimate_tokens(
            prompts.load("risk_tagging")
            + "\n\n".join(
                f"[{i}] {row.heading}\n{row.body}"
                for i, row in enumerate(rows[start : start + s.risk_tag_batch_size])
            )
        )
        + 1024
        for start in range(0, count, s.risk_tag_batch_size)
    )
    console.print(
        f"needs_review rows: {count}; batches: {batches}; "
        f"estimated counted tokens: {estimated_tokens:,}"
    )


def _save(s: Settings, name: str, text: str) -> None:
    s.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = s.reports_dir / f"{name}-{stamp}.md"
    path.write_text(f"# {name} ({stamp})\n\n{text}\n", encoding="utf-8")
    console.print(Markdown(text))
    console.print(f"\nSaved to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ingestion")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "report", "progress"):
        p = sub.add_parser(name)
        p.add_argument("--universe", default="data/universe.csv")
        if name != "progress":
            p.add_argument("--sample", type=int, help=f"first N of {SAMPLE_TICKERS}")
            p.add_argument("--tickers", help="comma-separated tickers")
    run = sub.choices["run"]
    run.add_argument("--only", choices=STAGES)
    run.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="companies processed at once (each holds a parsed 10-K in memory)",
    )
    run.add_argument("--force", action="store_true", help="re-run stages already done")
    run.add_argument(
        "--needs-review",
        action="store_true",
        help="tag only risk factors currently marked needs_review",
    )
    run.add_argument(
        "--from-cache",
        action="store_true",
        help="apply unambiguous cached tags before sending remaining rows to the LLM",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="show the needs_review retag plan without creating clients or making calls",
    )
    run.add_argument("--refresh", action="store_true", help="re-download SEC submissions")
    run.add_argument(
        "--daily",
        action="store_true",
        help="print progress before/after; stop cleanly at the daily budgets",
    )
    run.add_argument(
        "--daily-budget",
        type=int,
        help="counted Groq tokens per model per rolling 24h (default from settings)",
    )
    run.add_argument("--batch-size", type=int, help="risk factors per tagging call")
    run.add_argument(
        "--mode", choices=["json_schema", "json_object"], help="structured-output mode for tagging"
    )
    run.add_argument(
        "--experiment-tickers", help="comma-separated tickers to also tag from headings only"
    )
    sub.choices["report"].add_argument("--section", choices=["all", "sectors"], default="all")
    exp = sub.add_parser("experiment")
    exp.add_argument("--tickers", required=True)
    exp.add_argument(
        "--configs", required=True, help="mode:batch, e.g. json_schema:12,json_object:25"
    )
    rev = sub.add_parser("review")
    rev.add_argument("action", choices=["export", "score"])
    rev.add_argument("--path", default=str(REVIEW_SHEET))
    rev.add_argument("--n", type=int, default=30)
    rev.add_argument("--sheet-version", help="label for the tags in the sheet, e.g. v2")
    sub.add_parser("replay-tag-cache", help="replay cached tag echoes without database writes")
    args = parser.parse_args()

    s = get_settings()
    if args.command == "run" and args.dry_run and not args.needs_review:
        parser.error("--dry-run requires --needs-review")
    if args.command == "run" and args.from_cache and not args.needs_review:
        parser.error("--from-cache requires --needs-review")
    overrides = {
        "groq_daily_token_budget": getattr(args, "daily_budget", None),
        "risk_tag_batch_size": getattr(args, "batch_size", None),
        "risk_tag_response_mode": getattr(args, "mode", None),
    }
    s = s.model_copy(update={k: v for k, v in overrides.items() if v is not None})
    configure_logging("WARNING")  # progress bar + report are the interactive output
    logging.getLogger("ingestion").setLevel(s.log_level)
    handler = {
        "run": _run,
        "report": _report,
        "experiment": _experiment,
        "review": _review,
        "progress": _progress,
        "replay-tag-cache": _replay_handler,
    }[args.command]
    sys.exit(aio.run(handler(args, s)))


if __name__ == "__main__":
    main()
