"""Incremental, non-LLM refresh of new 8-K filings and current news."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.db.schema import filings, news_articles
from core.embeddings import Embedder
from core.newsapi import Article, NewsApiClient, build_query, parse_articles
from core.vectorstore import FILING_CHUNK, NEWS_ARTICLE, ensure_collections
from ingestion.edgar.client import EdgarClient
from ingestion.edgar.submissions import FilingRef, load_recent_8ks, load_ticker_map
from ingestion.extract.html_clean import blocks_text, html_to_blocks
from ingestion.load import postgres as pg
from ingestion.load.chunker import chunk_text, embedding_input, make_header
from ingestion.load.weaviate import PendingObjects, insert_objects
from ingestion.pipeline import SAMPLE_TICKERS, UniverseRow, read_universe

EIGHT_K_MAX_CHARS = 30_000


@dataclass(frozen=True)
class RefreshPlan:
    ticker: str
    cik: int
    new_8ks: tuple[FilingRef, ...]
    news_request_needed: bool
    sec_requests: int


async def plan_company(
    engine: AsyncEngine,
    edgar: EdgarClient,
    news: NewsApiClient | None,
    ticker: str,
    cik: int,
    *,
    today: date,
    dry_run: bool,
    news_cap: int,
    reserved_news_requests: int = 0,
) -> RefreshPlan:
    refs = await load_recent_8ks(
        edgar, cik, since=today - timedelta(days=365), refresh=True, cache=not dry_run
    )
    async with engine.connect() as conn:
        stored_accessions = set(
            (
                await conn.execute(
                    select(filings.c.accession_no).where(
                        filings.c.cik == cik, filings.c.form_type == "8-K"
                    )
                )
            ).scalars()
        )
    new_refs = tuple(ref for ref in refs if ref.accession_no not in stored_accessions)
    news_needed = bool(
        news
        and not news.cached_today(ticker)
        and news.requests_today() + reserved_news_requests < news_cap
    )
    return RefreshPlan(
        ticker=ticker,
        cik=cik,
        new_8ks=new_refs,
        news_request_needed=news_needed,
        sec_requests=1 + len(new_refs),
    )


async def refresh_company(
    engine: AsyncEngine,
    edgar: EdgarClient,
    news: NewsApiClient | None,
    embedder: Embedder | None,
    weaviate: Any,
    plan: RefreshPlan,
    *,
    company_name: str,
    today: date,
    dry_run: bool,
    news_cap: int,
) -> dict[str, int | str]:
    if dry_run:
        return {
            "ticker": plan.ticker,
            "new_8ks": len(plan.new_8ks),
            "news_requests": int(plan.news_request_needed),
            "sec_requests": plan.sec_requests,
        }
    if embedder is None or weaviate is None:
        raise ValueError("live incremental refresh requires embedding and Weaviate clients")

    pending_8k = PendingObjects(FILING_CHUNK)
    for ref in plan.new_8ks:
        body = blocks_text(html_to_blocks(await edgar.get_bytes(ref.url)))[:EIGHT_K_MAX_CHARS]
        await pg.upsert_filing(engine, plan.cik, ref, None)
        await pg.upsert_eight_k(engine, plan.cik, ref, body)
        _add_8k(pending_8k, plan.ticker, company_name, plan.cik, ref, body, embedder)

    pending_news = PendingObjects(NEWS_ARTICLE)
    if news is not None and not news.cached_today(plan.ticker) and news.requests_today() < news_cap:
        payload = await news.everything(
            plan.ticker,
            build_query(company_name, plan.ticker),
            29,
            today=today,
        )
        articles = parse_articles(payload)
        async with engine.connect() as conn:
            existing = set(
                (
                    await conn.execute(
                        select(news_articles.c.id).where(
                            news_articles.c.cik == plan.cik,
                            news_articles.c.id.in_([a.id for a in articles]),
                        )
                    )
                ).scalars()
            )
        await pg.upsert_news(engine, plan.cik, articles)
        for article in articles:
            if article.id not in existing:
                _add_news(pending_news, plan.ticker, company_name, plan.cik, article)

    await ensure_collections(weaviate)
    counts: dict[str, int | str] = {
        "ticker": plan.ticker,
        "new_8ks": len(plan.new_8ks),
        "news_requests": int(plan.news_request_needed),
        "sec_requests": plan.sec_requests,
    }
    for pending in (pending_8k, pending_news):
        if pending.keys:
            counts[pending.collection] = await insert_objects(
                weaviate, pending, await embedder.embed(pending.texts)
            )
    return counts


def _add_8k(
    pending: PendingObjects,
    ticker: str,
    company_name: str,
    cik: int,
    ref: FilingRef,
    body: str,
    embedder: Embedder,
) -> None:
    base = {
        "ticker": ticker,
        "cik": cik,
        "company": company_name,
        "sector": "",
    }
    header = make_header(
        embedder.tokenizer,
        ticker,
        "8-K",
        ref.filing_date.isoformat(),
        "Items " + ", ".join(ref.items),
    )
    for chunk in chunk_text(body, embedder.tokenizer):
        pending.add(
            f"{ref.accession_no}:8k:{chunk.index}",
            {
                **base,
                "text": chunk.text,
                "source_url": ref.url,
                "form_type": "8-K",
                "fiscal_year": ref.filing_date.year,
                "filing_date": _dt(ref.filing_date),
                "section": "8k_body",
                "item_codes": list(ref.items),
                "accession_no": ref.accession_no,
                "chunk_index": chunk.index,
            },
            embedding_input(header, chunk.text),
        )


def _add_news(
    pending: PendingObjects, ticker: str, company_name: str, cik: int, article: Article
) -> None:
    pending.add(
        article.id,
        {
            "ticker": ticker,
            "cik": cik,
            "company": company_name,
            "sector": "",
            "title": article.title,
            "description": article.description or "",
            "content": article.content_snippet or "",
            "source_name": article.source_name or "",
            "published_at": article.published_at,
            "source_url": article.url,
            "article_id": article.id,
        },
        f"{article.title}\n{article.description or ''}",
    )


def _dt(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


async def refresh_selected(
    engine: AsyncEngine,
    settings: Settings,
    *,
    sample: int | None,
    tickers: str | None,
    dry_run: bool,
) -> list[dict[str, int | str]]:
    universe = {r.ticker: r for r in read_universe(Path("data/universe.csv"))}
    wanted = (
        [t.strip().upper() for t in tickers.split(",") if t.strip()]
        if tickers
        else list(SAMPLE_TICKERS[:sample])
        if sample
        else list(universe)
    )
    engine_edgar = EdgarClient(
        settings.sec_user_agent, settings.raw_dir, settings.sec_requests_per_second
    )
    news = (
        NewsApiClient(
            settings.newsapi_key.get_secret_value(), settings.newsapi_base_url, settings.raw_dir
        )
        if settings.newsapi_key
        else None
    )
    wv = None
    embedder = None
    if not dry_run:
        from core.vectorstore import create_client, ensure_ready

        wv = create_client(settings)
        await ensure_ready(wv)
        embedder = Embedder(settings.embedding_model, settings.models_dir)
    try:
        cik_map = await load_ticker_map(engine_edgar, refresh=True, cache=not dry_run)
        results: list[dict[str, int | str]] = []
        today = datetime.now(UTC).date()
        reserved_news_requests = 0
        for ticker in wanted:
            row: UniverseRow = universe[ticker]
            plan = await plan_company(
                engine,
                engine_edgar,
                news,
                ticker,
                cik_map[ticker],
                today=today,
                dry_run=dry_run,
                news_cap=settings.newsapi_requests_per_day,
                reserved_news_requests=reserved_news_requests,
            )
            reserved_news_requests += int(plan.news_request_needed)
            if dry_run:
                result = await refresh_company(
                    engine,
                    engine_edgar,
                    news,
                    None,
                    None,
                    plan,
                    company_name=row.name,
                    today=today,
                    dry_run=True,
                    news_cap=settings.newsapi_requests_per_day,
                )
            else:
                result = await refresh_company(
                    engine,
                    engine_edgar,
                    news,
                    embedder,
                    wv,
                    plan,
                    company_name=row.name,
                    today=today,
                    dry_run=False,
                    news_cap=settings.newsapi_requests_per_day,
                )
            results.append(result)
        return results
    finally:
        await engine_edgar.aclose()
        if news:
            await news.aclose()
        if wv:
            await wv.close()
