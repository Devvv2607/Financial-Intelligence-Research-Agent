"""Helpers shared by tools. Nothing here assumes the full 205-company universe: tools
work with whatever companies are in the database and say which ones are missing."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Executable, select

from agent.tools.base import ToolContext, ToolFailure
from core.db.schema import companies


@dataclass(frozen=True)
class Company:
    cik: int
    ticker: str
    name: str
    sector: str | None
    fiscal_year_end: str | None


async def resolve_tickers(
    ctx: ToolContext, tickers: list[str] | None
) -> tuple[list[Company], list[str]]:
    """Known companies for the requested tickers (all companies if None) + unknown tickers.

    Raises not_found when none of the requested tickers is ingested, listing what is.
    """
    async with ctx.engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    companies.c.cik,
                    companies.c.ticker,
                    companies.c.name,
                    companies.c.llm_sector,
                    companies.c.sic_sector,
                    companies.c.fiscal_year_end,
                ).order_by(companies.c.ticker)
            )
        ).all()
    by_ticker = {
        r.ticker: Company(r.cik, r.ticker, r.name, r.llm_sector or r.sic_sector, r.fiscal_year_end)
        for r in rows
    }
    if not tickers:
        return list(by_ticker.values()), []
    wanted = [t.strip().upper() for t in tickers if t.strip()]
    known = [by_ticker[t] for t in wanted if t in by_ticker]
    missing = [t for t in wanted if t not in by_ticker]
    if not known:
        raise ToolFailure(
            "not_found",
            f"no ingested data for {', '.join(missing)}. Available tickers: "
            + ", ".join(by_ticker),
        )
    return known, missing


def missing_warning(missing: list[str]) -> list[str]:
    return [f"no ingested data for: {', '.join(missing)}"] if missing else []


def filing_index_url(cik: int, accession_no: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no.replace('-', '')}/"


async def fetch_dicts(ctx: ToolContext, stmt: Executable) -> list[dict[str, Any]]:
    """Run a read query and return plain dict rows (typed, JSON-friendly)."""
    async with ctx.engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(stmt)).mappings().all()]
