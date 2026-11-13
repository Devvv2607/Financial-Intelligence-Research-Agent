"""Read-only comparison of cached filings with the current HTML extractor."""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db.schema import companies, filings, risk_factors
from ingestion.extract.html_clean import html_to_blocks
from ingestion.extract.risk_splitter import split_risk_factors
from ingestion.extract.sections import find_risk_factors_titled, find_section_regex

GLUED_EXAMPLES = frozenset({"thecompany", "riskof", "aresubject", "toensure", "financialresults"})


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
    glued_before: int
    glued_after: int
    examples: list[GluedWordExample]
    raw_missing: bool = False


def cached_path(raw_dir: Path, url: str) -> Path:
    parsed = urlparse(url)
    return raw_dir / "sec" / parsed.netloc / parsed.path.lstrip("/")


def _has_glued_word(text: str) -> bool:
    return any(example in text.lower() for example in GLUED_EXAMPLES) or bool(
        re.search(r"[a-z][A-Z]", text)
    )


def extract_risk_factors(content: bytes) -> list[tuple[str, str]]:
    blocks = html_to_blocks(content)
    section = find_section_regex(blocks, "item_1a") or find_risk_factors_titled(blocks)
    if section is None:
        return []
    return [(risk.heading, risk.body) for risk in split_risk_factors(section.body(blocks))]


def _heading_key(text: str) -> str:
    return "".join(char.lower() for char in text if char.isalnum())


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
        return FilingComparison(ticker, accession_no, len(stored), 0, 0, 0, 0, 0, [], True)
    fixed = extract_risk_factors(path.read_bytes())
    before_by_key: dict[str, list[tuple[str, str]]] = {}
    after_by_key: dict[str, list[tuple[str, str]]] = {}
    for item in stored:
        before_by_key.setdefault(_heading_key(item[0]), []).append(item)
    for item in fixed:
        after_by_key.setdefault(_heading_key(item[0]), []).append(item)
    matched = {
        key
        for key in before_by_key.keys() & after_by_key.keys()
        if len(before_by_key[key]) == 1 and len(after_by_key[key]) == 1
    }
    heading_examples = [
        GluedWordExample(ticker, accession_no, "heading", before[0], after[0])
        for key in matched
        for before, after in [(before_by_key[key][0], after_by_key[key][0])]
        if before[0] != after[0]
    ]
    body_examples = [
        GluedWordExample(ticker, accession_no, "body", before[1], after[1])
        for key in matched
        for before, after in [(before_by_key[key][0], after_by_key[key][0])]
        if before[1] != after[1]
    ]
    return FilingComparison(
        ticker,
        accession_no,
        len(stored),
        len(fixed),
        len(heading_examples),
        len(body_examples),
        sum(_has_glued_word(heading) for heading, _ in stored),
        sum(_has_glued_word(heading) for heading, _ in fixed),
        (heading_examples + body_examples)[:10],
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
