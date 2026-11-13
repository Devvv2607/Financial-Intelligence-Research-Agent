"""news_agent tool: store-first news search with a guarded live NewsAPI fallback.

The store (Postgres + Weaviate, filled by ingestion) is always searched. NewsAPI is
called only for companies whose newest stored article is older than STALE_AFTER, only
when live fallback is enabled (never in production, D-010), and only within the shared
daily request cap. Any NewsAPI failure degrades to store results plus a warning.
"""

import re
from datetime import UTC, date, datetime, timedelta

import numpy as np
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from weaviate.classes.data import DataObject
from weaviate.classes.query import Filter
from weaviate.util import generate_uuid5

from agent.tools.base import Evidence, Output, ToolContext, ToolFailure, snippet, tool
from agent.tools.common import Company, missing_warning, resolve_tickers
from agent.tools.news_relevance import relevance
from core.db.schema import news_articles
from core.newsapi import Article, NewsApiError, build_query, parse_articles
from core.vectorstore import NEWS_ARTICLE

STALE_AFTER = timedelta(days=3)
MAX_LIVE_FETCHES = 3  # per call: protects the shared 100/day NewsAPI quota
CANDIDATES = 50  # hybrid-search candidates to gate and re-score


class SearchNewsInput(BaseModel):
    query: str = Field(min_length=2)
    tickers: list[str] | None = None
    since: date | None = Field(None, description="Default: 30 days ago")
    limit: int = Field(10, ge=1, le=25)


class NewsHit(BaseModel):
    source_id: str
    ticker: str
    title: str
    description: str | None
    source_name: str | None
    published_at: datetime
    url: str
    score: float | None = Field(description="(0.75*semantic + 0.25*keyword) * source_weight")
    score_parts: dict[str, float] = Field(default_factory=dict)
    matched_on: str | None = Field(None, description="where the company was named, e.g. title:name")


class NewsFreshness(BaseModel):
    latest_stored_date: date | None
    fetched_at: datetime


class SearchNewsOutput(BaseModel):
    hits: list[NewsHit]
    live_fetched: list[str] = Field(description="Tickers refreshed from NewsAPI in this call")
    freshness: dict[str, NewsFreshness] = Field(
        description="Newest stored article date and freshness-check time per company"
    )


def stale_companies(latest: dict[int, datetime | None], now: datetime) -> list[int]:
    return [cik for cik, ts in latest.items() if ts is None or now - ts > STALE_AFTER]


async def _latest_published(ctx: ToolContext, ciks: list[int]) -> dict[int, datetime | None]:
    async with ctx.engine.connect() as conn:
        rows = (
            await conn.execute(
                select(news_articles.c.cik, func.max(news_articles.c.published_at))
                .where(news_articles.c.cik.in_(ciks))
                .group_by(news_articles.c.cik)
            )
        ).all()
    found = {r[0]: r[1] for r in rows}
    return {c: found.get(c) for c in ciks}


async def _store(ctx: ToolContext, company: Company, articles: list[Article]) -> None:
    """Persist live results exactly as ingestion would (same ids => idempotent)."""
    if not articles:
        return
    async with ctx.engine.begin() as conn:
        await conn.execute(
            insert(news_articles)
            .values(
                [
                    {
                        "id": a.id,
                        "cik": company.cik,
                        "title": a.title,
                        "description": a.description,
                        "content_snippet": a.content_snippet,
                        "url": a.url,
                        "source_name": a.source_name,
                        "published_at": a.published_at,
                    }
                    for a in articles
                ]
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
    vectors = await ctx.embedder.embed([f"{a.title}\n{a.description or ''}" for a in articles])  # type: ignore[union-attr]
    col = ctx.weaviate.collections.get(NEWS_ARTICLE)  # type: ignore[union-attr]
    await col.data.insert_many(
        [
            DataObject(
                uuid=generate_uuid5(f"{NEWS_ARTICLE}:{a.id}"),
                vector=vec.tolist(),
                properties={
                    "title": a.title,
                    "description": a.description or "",
                    "content": a.content_snippet or "",
                    "ticker": company.ticker,
                    "cik": company.cik,
                    "company": company.name,
                    "sector": company.sector or "",
                    "source_name": a.source_name or "",
                    "published_at": a.published_at,
                    "source_url": a.url,
                    "article_id": a.id,
                },
            )
            for a, vec in zip(articles, vectors, strict=True)
        ]
    )


async def _live_refresh(ctx: ToolContext, stale: list[Company]) -> tuple[list[str], list[str]]:
    fetched: list[str] = []
    warnings: list[str] = []
    news = ctx.news
    if not stale:
        return fetched, warnings
    if news is None or not ctx.settings.news_live_fallback_enabled:
        warnings.append(
            "stored news may be stale for "
            + ", ".join(c.ticker for c in stale)
            + " (live NewsAPI fallback disabled)"
        )
        return fetched, warnings
    for company in stale[:MAX_LIVE_FETCHES]:
        if (
            not news.cached_today(company.ticker)
            and news.requests_today() >= ctx.settings.newsapi_requests_per_day
        ):
            warnings.append("NewsAPI daily request cap reached; using stored news only")
            break
        try:
            payload = await news.everything(
                company.ticker,
                build_query(company.name, company.ticker),
                ctx.settings.news_window_days,
            )
            existing = await _existing_ids(ctx, payload)
            await _store(ctx, company, [a for a in parse_articles(payload) if a.id not in existing])
            fetched.append(company.ticker)
        except (NewsApiError, TimeoutError, OSError) as exc:
            warnings.append(
                f"news unavailable for {company.ticker} ({type(exc).__name__}); "
                "answering from stored news"
            )
    return fetched, warnings


async def _existing_ids(ctx: ToolContext, payload: dict[str, object]) -> set[str]:
    ids = [a.id for a in parse_articles(payload)]
    if not ids:
        return set()
    async with ctx.engine.connect() as conn:
        return set(
            (
                await conn.execute(select(news_articles.c.id).where(news_articles.c.id.in_(ids)))
            ).scalars()
        )


@tool("search_news", SearchNewsInput, timeout_s=45)
async def search_news(ctx: ToolContext, inp: SearchNewsInput) -> Output:
    """Search recent news about companies (headlines and descriptions, newest data first).
    Reads the news store; refreshes from NewsAPI only when stored news is stale and live
    fallback is allowed. Use to check whether recent events support filing disclosures."""
    if ctx.weaviate is None or ctx.embedder is None:
        raise ToolFailure("internal", "vector search is not configured")
    known, missing = await resolve_tickers(ctx, inp.tickers)
    now = datetime.now(UTC)
    since = inp.since or (now - timedelta(days=30)).date()
    latest = await _latest_published(ctx, [c.cik for c in known])
    stale_ciks = set(stale_companies(latest, now))
    fetched, warnings = await _live_refresh(ctx, [c for c in known if c.cik in stale_ciks])

    vector = (await ctx.embedder.embed([inp.query]))[0]
    col = ctx.weaviate.collections.get(NEWS_ARTICLE)
    # Hybrid search only *collects candidates*; ranking uses our absolute score
    # (news_relevance), because hybrid fusion scores are relative to each result list.
    res = await col.query.hybrid(
        query=inp.query,
        vector=vector.tolist(),
        alpha=0.5,
        limit=CANDIDATES,
        include_vector=True,
        filters=Filter.all_of(
            [
                Filter.by_property("ticker").contains_any([c.ticker for c in known]),
                Filter.by_property("published_at").greater_or_equal(
                    datetime(since.year, since.month, since.day, tzinfo=UTC)
                ),
            ]
        ),
    )
    by_ticker = {c.ticker: c for c in known}
    scored: list[NewsHit] = []
    gated_out = 0
    for o in res.objects:
        p = o.properties
        company = by_ticker[str(p["ticker"])]
        article_vec = np.asarray(o.vector["default"], dtype=np.float32)
        rel = relevance(
            inp.query,
            str(p["title"]),
            str(p.get("description") or ""),
            str(p.get("source_name") or ""),
            float(vector @ article_vec),
            company.name,
            company.ticker,
        )
        if rel is None:
            gated_out += 1
            continue
        scored.append(
            NewsHit(
                source_id=f"news:{p['article_id']}",
                ticker=company.ticker,
                title=str(p["title"]),
                description=str(p.get("description") or "") or None,
                source_name=str(p.get("source_name") or "") or None,
                published_at=p["published_at"],
                url=str(p["source_url"]),
                score=rel.score,
                score_parts={
                    "semantic": rel.semantic,
                    "keyword": rel.keyword,
                    "source_weight": rel.source_weight,
                },
                matched_on=rel.matched_on,
            )
        )
    hits: list[NewsHit] = []
    seen_titles: set[str] = set()
    for h in sorted(scored, key=lambda h: -(h.score or 0)):
        key = re.sub(r"\W+", " ", h.title.lower()).strip()
        if key in seen_titles:  # syndicated copies of the same story
            continue
        seen_titles.add(key)
        hits.append(h)
        if len(hits) == inp.limit:
            break
    evidence = [
        Evidence(
            source_id=h.source_id,
            source_type="news",
            ticker=h.ticker,
            title=h.title,
            snippet=snippet(f"{h.title}. {h.description or ''}"),
            url=h.url,
            filed_or_published=h.published_at,
            score=h.score,
        )
        for h in hits
    ]
    if gated_out:
        warnings.append(
            f"{gated_out} candidate articles dropped (company not named, not a news source, "
            "or below the relevance floor)"
        )
    warnings = missing_warning(missing) + warnings + ([] if hits else ["no relevant news found"])
    fetched_at = datetime.now(UTC)
    freshness = {
        company.ticker: NewsFreshness(
            latest_stored_date=(stored.date() if (stored := latest[company.cik]) else None),
            fetched_at=fetched_at,
        )
        for company in known
    }
    for ticker, metadata in freshness.items():
        warnings.append(
            f"news stored through {metadata.latest_stored_date or 'no date'} for {ticker}; "
            f"fetched at {fetched_at.isoformat()}"
        )
    return Output(
        SearchNewsOutput(
            hits=hits,
            live_fetched=fetched,
            freshness=freshness,
        ),
        evidence,
        warnings,
    )
