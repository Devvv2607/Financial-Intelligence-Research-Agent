"""Read-only comparison of cached filings with the current HTML extractor."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db.schema import companies, filings, risk_factors
from ingestion.extract.html_clean import html_to_blocks
from ingestion.extract.risk_splitter import split_risk_factors


@dataclass(frozen=True)
class GluedWordExample:
    ticker: str
    accession_no: str
    field: str
    before: str
    after: str


@dataclass(frozen=True)
class FilingComparison:
    ticker: str
    accession_no: str
    stored_count: int
    fixed_count: int
    heading_changes: int
    body_changes: int
    examples: list[GluedWordExample]
    raw_missing: bool = False


def cached_path(raw_dir: Path, url: str) -> Path:
    parsed = urlparse(url)
    return raw_dir / "sec" / parsed.netloc / parsed.path.lstrip("/")


def compare_filing(
    *,
    ticker: str,
    accession_no: str,
    url: str,
    raw_dir: Path,
    stored: list[tuple[str, str]],
) -> FilingComparison:
    path = cached_path(raw_dir, url)
    if not path.exists():
        return FilingComparison(ticker, accession_no, len(stored), 0, 0, 0, [], True)
    fixed = split_risk_factors(html_to_blocks(path.read_bytes()))
    stored_pairs = [(heading, body) for heading, body in stored]
    fixed_pairs = [(risk.heading, risk.body) for risk in fixed]
    heading_changes = sum(
        before != after for (before, _), (after, _) in zip(stored_pairs, fixed_pairs, strict=False)
    )
    body_changes = sum(
        before != after for (_, before), (_, after) in zip(stored_pairs, fixed_pairs, strict=False)
    )
    examples = [
        GluedWordExample(ticker, accession_no, field, before, after)
        for field, before_values, after_values in (
            ("heading", [x[0] for x in stored_pairs], [x[0] for x in fixed_pairs]),
            ("body", [x[1] for x in stored_pairs], [x[1] for x in fixed_pairs]),
        )
        for before, after in zip(before_values, after_values, strict=False)
        if before != after
    ][:10]
    return FilingComparison(
        ticker,
        accession_no,
        len(stored_pairs),
        len(fixed_pairs),
        heading_changes,
        body_changes,
        examples,
    )


async def compare_cached_filings(engine: AsyncEngine, raw_dir: Path) -> list[FilingComparison]:
    async with engine.connect() as conn:
        filing_rows = (
            await conn.execute(
                select(filings.c.accession_no, filings.c.url, companies.c.ticker)
                .join(companies, companies.c.cik == filings.c.cik)
                .where(filings.c.form_type == "10-K")
                .order_by(companies.c.ticker, filings.c.filing_date)
            )
        ).all()
        risk_rows = (
            await conn.execute(
                select(
                    risk_factors.c.accession_no,
                    risk_factors.c.ordinal,
                    risk_factors.c.heading,
                    risk_factors.c.body,
                ).order_by(risk_factors.c.accession_no, risk_factors.c.ordinal)
            )
        ).all()
    stored_by_filing: dict[str, list[tuple[str, str]]] = {}
    for row in risk_rows:
        stored_by_filing.setdefault(row.accession_no, []).append((row.heading, row.body))
    return [
        compare_filing(
            ticker=ticker,
            accession_no=accession_no,
            url=url,
            raw_dir=raw_dir,
            stored=stored_by_filing.get(accession_no, []),
        )
        for accession_no, url, ticker in filing_rows
    ]
