"""Zero-token replay and planning for cached risk-tag responses."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db.schema import companies, llm_cache, risk_factors
from ingestion.enrich.risk_tagger import VALID_CATEGORIES, TagResult, _words


@dataclass(frozen=True)
class ReplayRow:
    key: str
    heading: str
    ticker: str = ""


@dataclass(frozen=True)
class CachedTagPlan:
    tags: list[TagResult]
    samples: list[tuple[str, str, str]]
    cached_echoes: int


def build_cached_tag_plan(
    flagged: list[ReplayRow],
    all_rows: list[ReplayRow],
    responses: list[Any],
) -> CachedTagPlan:
    """Build a conservative cache plan without guessing row associations."""
    by_prefix: dict[str, list[str]] = defaultdict(list)
    for row in all_rows:
        words = _words(row.heading, 6)
        for length in range(1, len(words.split()) + 1):
            by_prefix[" ".join(words.split()[:length])].append(row.key)

    flagged_ids = {row.key for row in flagged}
    candidates: dict[str, list[tuple[str, str]]] = defaultdict(list)
    cached_echoes = 0
    for response in responses:
        response_tags: Any = response.get("tags") if isinstance(response, dict) else None
        if not isinstance(response_tags, list):
            continue
        for tag in response_tags:
            if not isinstance(tag, dict):
                continue
            echo = _words(str(tag.get("heading_start") or ""), 6)
            if not echo:
                continue
            cached_echoes += 1
            category = tag.get("category")
            matches = by_prefix.get(echo, [])
            if category in VALID_CATEGORIES and len(matches) == 1 and matches[0] in flagged_ids:
                candidates[matches[0]].append((category, str(tag.get("summary") or "").strip()))

    tags: list[TagResult] = []
    samples: list[tuple[str, str, str]] = []
    for row in flagged:
        options = candidates.get(row.key, [])
        if len(options) != 1:
            continue
        category, summary = options[0]
        tags.append(TagResult(row.key, category, summary, "cache"))
        samples.append((row.ticker, row.heading, category))
    return CachedTagPlan(tags, samples, cached_echoes)


async def load_cached_tag_plan(engine: AsyncEngine) -> CachedTagPlan:
    async with engine.connect() as conn:
        flagged = [
            ReplayRow(r.id, r.heading, r.ticker)
            for r in (
                await conn.execute(
                    select(
                        risk_factors.c.id,
                        risk_factors.c.heading,
                        companies.c.ticker,
                    )
                    .join(companies, companies.c.cik == risk_factors.c.cik)
                    .where(risk_factors.c.needs_review.is_(True))
                )
            ).all()
        ]
        all_rows = [
            ReplayRow(r.id, r.heading)
            for r in (await conn.execute(select(risk_factors.c.id, risk_factors.c.heading))).all()
        ]
        responses = list(
            (
                await conn.execute(
                    select(llm_cache.c.response).where(llm_cache.c.response.has_key("tags"))
                )
            ).scalars()
        )
    return build_cached_tag_plan(flagged, all_rows, responses)


async def replay_cached_tags(engine: AsyncEngine) -> dict[str, int]:
    """Count flagged rows with an unambiguous cached normalized-prefix echo.

    The response cache does not retain the original batch listing or CIK. Rows are
    credited only when the echo has exactly one matching heading across the sample.
    """
    async with engine.connect() as conn:
        flagged = (
            await conn.execute(
                select(risk_factors.c.id, risk_factors.c.heading).where(
                    risk_factors.c.needs_review.is_(True)
                )
            )
        ).all()
        all_rows = (await conn.execute(select(risk_factors.c.id, risk_factors.c.heading))).all()
        cached = (
            await conn.execute(
                select(llm_cache.c.response).where(llm_cache.c.response.has_key("tags"))
            )
        ).scalars()

    plan = build_cached_tag_plan(
        [ReplayRow(risk_id, heading) for risk_id, heading in flagged],
        [ReplayRow(risk_id, heading) for risk_id, heading in all_rows],
        list(cached),
    )

    return {
        "flagged_rows": len(flagged),
        "cached_echoes": plan.cached_echoes,
        "would_validate": len(plan.tags),
        "not_creditable": len(flagged) - len(plan.tags),
    }
