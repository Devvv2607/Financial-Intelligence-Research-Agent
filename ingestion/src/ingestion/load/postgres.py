"""Idempotent Postgres writes for the pipeline.

Every write is an upsert on a natural key (CIK, accession number, content hash), so a
re-run converges to the same rows instead of duplicating them. Risk-factor upserts never
overwrite tag columns: re-extracting an unchanged risk factor keeps its tag for free.
"""

import hashlib
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db.schema import (
    companies,
    eight_k_events,
    filing_sections,
    filings,
    financial_facts,
    ingestion_jobs,
    news_articles,
    risk_factors,
    risk_tag_experiments,
)
from ingestion.edgar.submissions import CompanyMeta, FilingRef
from ingestion.edgar.xbrl import FactRow
from ingestion.enrich.risk_tagger import TagResult
from ingestion.extract.risk_splitter import RiskFactor
from ingestion.news.client import Article


def risk_factor_id(cik: int, fiscal_year: int, heading: str, body: str) -> str:
    return hashlib.sha256(f"{cik}|{fiscal_year}|{heading}|{body}".encode()).hexdigest()[:32]


async def _upsert(
    engine: AsyncEngine,
    table: Any,
    rows: Sequence[dict[str, Any]],
    keys: list[str],
    update_cols: Iterable[str] | None = None,
) -> None:
    if not rows:
        return
    cols = list(update_cols) if update_cols is not None else [c for c in rows[0] if c not in keys]
    async with engine.begin() as conn:
        # Postgres caps bind parameters per statement, so insert in batches of 500 rows.
        for i in range(0, len(rows), 500):
            stmt = insert(table).values(list(rows[i : i + 500]))
            stmt = (
                stmt.on_conflict_do_update(
                    index_elements=keys, set_={c: stmt.excluded[c] for c in cols}
                )
                if cols
                else stmt.on_conflict_do_nothing(index_elements=keys)
            )
            await conn.execute(stmt)


async def ensure_company(
    engine: AsyncEngine, cik: int, ticker: str, name: str, universe_sector: str | None
) -> None:
    """Minimal row so jobs can be recorded even if EDGAR fails before metadata loads."""
    await _upsert(
        engine,
        companies,
        [{"cik": cik, "ticker": ticker, "name": name, "universe_sector": universe_sector}],
        ["cik"],
        update_cols=[],
    )


async def upsert_company(
    engine: AsyncEngine,
    meta: CompanyMeta,
    ticker: str,
    universe_sector: str | None,
    sic_sector: str | None,
) -> None:
    await _upsert(
        engine,
        companies,
        [
            {
                "cik": meta.cik,
                "ticker": ticker,
                "name": meta.name,
                "universe_sector": universe_sector,
                "sic": meta.sic,
                "sic_description": meta.sic_description,
                "exchanges": list(meta.exchanges),
                "fiscal_year_end": meta.fiscal_year_end,
                "sic_sector": sic_sector,
            }
        ],
        ["cik"],
    )


async def upsert_filing(
    engine: AsyncEngine, cik: int, ref: FilingRef, fiscal_year: int | None
) -> None:
    await _upsert(
        engine,
        filings,
        [
            {
                "accession_no": ref.accession_no,
                "cik": cik,
                "form_type": ref.form_type,
                "filing_date": ref.filing_date,
                "report_date": ref.report_date,
                "fiscal_year": fiscal_year,
                "primary_doc": ref.primary_doc,
                "url": ref.url,
                "items": list(ref.items) or None,
            }
        ],
        ["accession_no"],
    )


async def upsert_section(
    engine: AsyncEngine, accession_no: str, section: str, text: str, method: str
) -> None:
    await _upsert(
        engine,
        filing_sections,
        [
            {
                "accession_no": accession_no,
                "section": section,
                "text": text,
                "char_count": len(text),
                "method": method,
            }
        ],
        ["accession_no", "section"],
    )


async def replace_risk_factors(
    engine: AsyncEngine, cik: int, accession_no: str, fiscal_year: int, rfs: list[RiskFactor]
) -> list[str]:
    """Store this filing's risk factors; drop ones no longer present. Returns ids."""
    rows = [
        {
            "id": risk_factor_id(cik, fiscal_year, r.heading, r.body),
            "accession_no": accession_no,
            "cik": cik,
            "fiscal_year": fiscal_year,
            "ordinal": r.ordinal,
            "subsection": r.subsection,
            "heading": r.heading,
            "body": r.body,
        }
        for r in rfs
    ]
    # Same content can (rarely) repeat inside one filing; keep the first occurrence.
    unique = list({row["id"]: row for row in reversed(rows)}.values())[::-1]
    ids = [str(row["id"]) for row in unique]
    async with engine.begin() as conn:
        await conn.execute(
            delete(risk_factors).where(
                risk_factors.c.accession_no == accession_no, risk_factors.c.id.notin_(ids)
            )
        )
    await _upsert(
        engine,
        risk_factors,
        unique,
        ["id"],
        update_cols=["accession_no", "ordinal", "subsection"],
    )
    return ids


async def upsert_eight_k(engine: AsyncEngine, cik: int, ref: FilingRef, body: str) -> None:
    await _upsert(
        engine,
        eight_k_events,
        [
            {
                "accession_no": ref.accession_no,
                "cik": cik,
                "filing_date": ref.filing_date,
                "items": list(ref.items),
                "body": body,
            }
        ],
        ["accession_no"],
    )


async def upsert_facts(engine: AsyncEngine, cik: int, facts: list[FactRow]) -> None:
    await _upsert(
        engine,
        financial_facts,
        [
            {
                "cik": cik,
                "metric": f.metric,
                "fiscal_year": f.fiscal_year,
                "value": f.value,
                "unit": f.unit,
                "concept": f.concept,
                "period_start": f.period_start,
                "period_end": f.period_end,
                "accession_no": f.accession_no,
                "filed": f.filed,
            }
            for f in facts
        ],
        ["cik", "metric", "fiscal_year"],
    )


async def upsert_news(engine: AsyncEngine, cik: int, articles: list[Article]) -> None:
    await _upsert(
        engine,
        news_articles,
        [
            {
                "id": a.id,
                "cik": cik,
                "title": a.title,
                "description": a.description,
                "content_snippet": a.content_snippet,
                "url": a.url,
                "source_name": a.source_name,
                "published_at": a.published_at,
            }
            for a in articles
        ],
        ["id"],
    )


async def save_tags(engine: AsyncEngine, tags: list[TagResult]) -> None:
    async with engine.begin() as conn:
        for t in tags:
            await conn.execute(
                update(risk_factors)
                .where(risk_factors.c.id == t.key)
                .values(
                    category=t.category,
                    summary=t.summary,
                    tag_source=t.source,
                    inherited_from=t.inherited_from,
                    similarity=t.similarity,
                    needs_review=t.needs_review,
                )
            )


async def save_experiment_tags(engine: AsyncEngine, mode: str, tags: list[TagResult]) -> None:
    await _upsert(
        engine,
        risk_tag_experiments,
        [
            {
                "risk_factor_id": t.key,
                "mode": mode,
                "category": t.category,
                "summary": t.summary,
                "needs_review": t.needs_review,
            }
            for t in tags
        ],
        ["risk_factor_id", "mode"],
    )


async def save_sector_llm(
    engine: AsyncEngine,
    cik: int,
    sector: str,
    sub_industry: str,
    confidence: float,
    rationale: str,
) -> None:
    async with engine.begin() as conn:
        sic_sector = (
            await conn.execute(select(companies.c.sic_sector).where(companies.c.cik == cik))
        ).scalar_one_or_none()
        await conn.execute(
            update(companies)
            .where(companies.c.cik == cik)
            .values(
                llm_sector=sector,
                llm_sub_industry=sub_industry,
                llm_sector_confidence=confidence,
                llm_sector_rationale=rationale,
                sector_conflict=(sic_sector is not None and sic_sector != sector),
            )
        )


async def set_job(
    engine: AsyncEngine,
    cik: int,
    stage: str,
    status: str,
    *,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    stmt = insert(ingestion_jobs).values(
        cik=cik, stage=stage, status=status, error=error, details=details, attempts=1
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["cik", "stage"],
        set_={
            "status": stmt.excluded.status,
            "error": stmt.excluded.error,
            "details": stmt.excluded.details,
            "attempts": ingestion_jobs.c.attempts + (1 if status == "running" else 0),
        },
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)


async def job_status(engine: AsyncEngine, cik: int, stage: str) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                select(ingestion_jobs.c.status).where(
                    ingestion_jobs.c.cik == cik, ingestion_jobs.c.stage == stage
                )
            )
        ).scalar_one_or_none()


def ten_k_fiscal_year(ref: FilingRef) -> int:
    """Label a 10-K by its period end year (same convention as XBRL facts, D-004)."""
    period = ref.report_date or ref.filing_date
    return period.year if isinstance(period, date) else ref.filing_date.year
