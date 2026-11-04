"""filings_agent tools: hybrid search over filings, risk factors, YoY risk diff, 8-K events."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from weaviate.classes.query import Filter, MetadataQuery

from agent.tools.base import Evidence, Output, ToolContext, ToolFailure, snippet, tool
from agent.tools.common import fetch_dicts, missing_warning, resolve_tickers
from agent.tools.eight_k_items import describe_items, event_excerpt
from core.db.schema import eight_k_events, filings, risk_factors
from core.vectorstore import FILING_CHUNK

Section = Literal["item_1", "item_1a", "business_overview", "8k_body"]

# Calibrated on AAPL FY2025 vs FY2026 (bge-small cosine, heading + first 150 words):
# a genuinely new risk scored 0.84 against its closest old one, reworded risks 0.94-0.97,
# untouched ones >= 0.99. Also matches the 0.90 ingestion uses to reuse a tag (D-003).
MATCH_SIMILARITY = 0.90
UNCHANGED_SIMILARITY = 0.985
COMPARE_BODY_WORDS = 150


def _as_int(v: object) -> int | None:
    return v if isinstance(v, int) else None


def _as_date(v: object) -> date | None:
    """Weaviate returns DATE properties as datetimes."""
    return v.date() if isinstance(v, datetime) else None


def _need(ctx: ToolContext) -> None:
    if ctx.weaviate is None or ctx.embedder is None:
        raise ToolFailure("internal", "vector search is not configured")


# ---------------------------------------------------------------- search_filings
class SearchFilingsInput(BaseModel):
    query: str = Field(min_length=2, description="What to look for, in natural language")
    tickers: list[str] | None = Field(None, description="Limit to these tickers")
    form_types: list[Literal["10-K", "8-K"]] | None = None
    fiscal_years: list[int] | None = None
    sections: list[Section] | None = Field(
        None, description="item_1 = Business, item_1a = Risk Factors, 8k_body = 8-K text"
    )
    limit: int = Field(8, ge=1, le=20)
    alpha: float = Field(0.5, ge=0, le=1, description="0 = keyword (BM25) only, 1 = vector only")


class FilingHit(BaseModel):
    source_id: str
    ticker: str
    form_type: str
    fiscal_year: int | None
    section: str
    filing_date: date | None
    url: str
    text: str
    score: float | None


class SearchFilingsOutput(BaseModel):
    hits: list[FilingHit]


@tool("search_filings", SearchFilingsInput, timeout_s=20)
async def search_filings(ctx: ToolContext, inp: SearchFilingsInput) -> Output:
    """Hybrid (BM25 + vector) search over 10-K sections (Business, Risk Factors) and 8-K
    text, with optional filters on tickers, form types, fiscal years and sections. Use for
    open questions about what companies say in their filings."""
    _need(ctx)
    known, missing = await resolve_tickers(ctx, inp.tickers)
    filters = [Filter.by_property("ticker").contains_any([c.ticker for c in known])]
    if inp.form_types:
        filters.append(Filter.by_property("form_type").contains_any(list(inp.form_types)))
    if inp.fiscal_years:
        filters.append(Filter.by_property("fiscal_year").contains_any(inp.fiscal_years))
    if inp.sections:
        filters.append(Filter.by_property("section").contains_any(list(inp.sections)))
    vector = (await ctx.embedder.embed([inp.query]))[0]  # type: ignore[union-attr]
    col = ctx.weaviate.collections.get(FILING_CHUNK)  # type: ignore[union-attr]
    res = await col.query.hybrid(
        query=inp.query,
        vector=vector.tolist(),
        alpha=inp.alpha,
        limit=inp.limit,
        filters=Filter.all_of(filters),
        return_metadata=MetadataQuery(score=True),
    )
    hits = []
    for o in res.objects:
        p = o.properties
        hits.append(
            FilingHit(
                source_id=f"{p['accession_no']}:{p['section']}:{p['chunk_index']}",
                ticker=str(p["ticker"]),
                form_type=str(p["form_type"]),
                fiscal_year=_as_int(p.get("fiscal_year")),
                section=str(p["section"]),
                filing_date=_as_date(p.get("filing_date")),
                url=str(p["source_url"]),
                text=str(p["text"]),
                score=o.metadata.score,
            )
        )
    evidence = [
        Evidence(
            source_id=h.source_id,
            source_type="filing_chunk",
            ticker=h.ticker,
            title=f"{h.ticker} {h.form_type} FY{h.fiscal_year} {h.section}",
            snippet=snippet(h.text),
            url=h.url,
            form_type=h.form_type,
            fiscal_year=h.fiscal_year,
            section=h.section,
            filed_or_published=h.filing_date,
            score=h.score,
        )
        for h in hits
    ]
    warnings = missing_warning(missing) + ([] if hits else ["no matching filing text"])
    return Output(SearchFilingsOutput(hits=hits), evidence, warnings)


# ---------------------------------------------------------------- get_risk_factors
class GetRiskFactorsInput(BaseModel):
    ticker: str
    fiscal_year: int | None = Field(None, description="Defaults to the latest 10-K")
    category: str | None = Field(None, description="Taxonomy category, e.g. supply_chain")
    limit: int = Field(60, ge=1, le=100)


class RiskFactorOut(BaseModel):
    id: str
    ordinal: int
    subsection: str | None
    heading: str
    category: str | None
    summary: str | None
    needs_review: bool


class GetRiskFactorsOutput(BaseModel):
    ticker: str
    fiscal_year: int
    filing_url: str
    total: int
    category_counts: dict[str, int]
    risk_factors: list[RiskFactorOut]


async def _latest_years(ctx: ToolContext, cik: int) -> list[int]:
    async with ctx.engine.connect() as conn:
        years = (
            (
                await conn.execute(
                    select(risk_factors.c.fiscal_year).distinct().where(risk_factors.c.cik == cik)
                )
            )
            .scalars()
            .all()
        )
    return sorted(years, reverse=True)


@tool("get_risk_factors", GetRiskFactorsInput)
async def get_risk_factors(ctx: ToolContext, inp: GetRiskFactorsInput) -> Output:
    """List a company's tagged 10-K risk factors (heading, category, one-line summary) for a
    fiscal year (default: latest), optionally filtered to one risk category."""
    (company,), _ = await resolve_tickers(ctx, [inp.ticker])
    years = await _latest_years(ctx, company.cik)
    if not years:
        raise ToolFailure("not_found", f"no risk factors stored for {company.ticker}")
    year = inp.fiscal_year or years[0]
    if year not in years:
        raise ToolFailure("not_found", f"{company.ticker} has risk factors for {years}, not {year}")
    rows = await fetch_dicts(
        ctx,
        select(risk_factors, filings.c.url, filings.c.filing_date)
        .join(filings, filings.c.accession_no == risk_factors.c.accession_no)
        .where(risk_factors.c.cik == company.cik, risk_factors.c.fiscal_year == year)
        .order_by(risk_factors.c.ordinal),
    )
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["category"] or "untagged"] = counts.get(r["category"] or "untagged", 0) + 1
    picked = [r for r in rows if inp.category is None or r["category"] == inp.category][: inp.limit]
    out = GetRiskFactorsOutput(
        ticker=company.ticker,
        fiscal_year=year,
        filing_url=rows[0]["url"],
        total=len(rows),
        category_counts=dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        risk_factors=[
            RiskFactorOut(
                id=r["id"],
                ordinal=r["ordinal"],
                subsection=r["subsection"],
                heading=r["heading"],
                category=r["category"],
                summary=r["summary"],
                needs_review=r["needs_review"],
            )
            for r in picked
        ],
    )
    evidence = [_risk_evidence(company.ticker, r) for r in picked]
    warn = [] if picked else [f"no {inp.category} risk factors in FY{year}"]
    return Output(out, evidence, warn)


def _risk_evidence(ticker: str, r: Mapping[str, Any]) -> Evidence:
    return Evidence(
        source_id=f"rf:{r['id']}",
        source_type="risk_factor",
        ticker=ticker,
        title=f"{ticker} 10-K FY{r['fiscal_year']} risk factor: {r['heading']}"[:300],
        snippet=snippet(f"{r['heading']} {r['body']}"),
        url=str(r["url"]),
        form_type="10-K",
        fiscal_year=int(str(r["fiscal_year"])),
        section="item_1a",
        filed_or_published=r.get("filing_date"),
    )


# ---------------------------------------------------------------- compare_risk_factors
class CompareRiskFactorsInput(BaseModel):
    ticker: str
    year_a: int | None = Field(None, description="Earlier fiscal year (default: second latest)")
    year_b: int | None = Field(None, description="Later fiscal year (default: latest)")


class RiskRef(BaseModel):
    id: str
    heading: str
    category: str | None


class ChangedRisk(BaseModel):
    before: RiskRef
    after: RiskRef
    similarity: float
    category_changed: bool


class CompareRiskFactorsOutput(BaseModel):
    ticker: str
    year_a: int
    year_b: int
    new: list[RiskRef]
    removed: list[RiskRef]
    changed: list[ChangedRisk]
    unchanged_count: int
    category_counts_a: dict[str, int]
    category_counts_b: dict[str, int]


def match_risks(
    vecs_a: NDArray[np.float32], vecs_b: NDArray[np.float32]
) -> list[tuple[int, int, float]]:
    """Greedy one-to-one matching by descending cosine similarity, kept if >= threshold.

    Greedy is enough here: each year has ~20-60 risks and near-duplicates are rare; it is
    deterministic and easy to explain, unlike an assignment solver's global trade-offs.
    """
    if len(vecs_a) == 0 or len(vecs_b) == 0:
        return []
    sims = vecs_a @ vecs_b.T
    pairs = sorted(
        ((i, j, float(sims[i, j])) for i in range(len(vecs_a)) for j in range(len(vecs_b))),
        key=lambda p: -p[2],
    )
    used_a: set[int] = set()
    used_b: set[int] = set()
    out = []
    for i, j, s in pairs:
        if s < MATCH_SIMILARITY:
            break
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        out.append((i, j, s))
    return out


def _compare_text(r: Mapping[str, Any]) -> str:
    return f"{r['heading']}\n{' '.join(str(r['body']).split()[:COMPARE_BODY_WORDS])}"


def _ref(r: Mapping[str, Any]) -> RiskRef:
    return RiskRef(id=r["id"], heading=r["heading"], category=r["category"])


def _category_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        cat = r["category"] or "untagged"
        counts[cat] = counts.get(cat, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


@tool("compare_risk_factors", CompareRiskFactorsInput, timeout_s=45)
async def compare_risk_factors(ctx: ToolContext, inp: CompareRiskFactorsInput) -> Output:
    """Compare a company's risk factors between two 10-Ks: which risks are new, removed,
    or changed (reworded, or moved to another category). Uses embedding similarity, so
    reworded risks are recognised as the same risk."""
    if ctx.embedder is None:
        raise ToolFailure("internal", "embedder is not configured")
    (company,), _ = await resolve_tickers(ctx, [inp.ticker])
    years = await _latest_years(ctx, company.cik)
    if len(years) < 2 and (inp.year_a is None or inp.year_b is None):
        raise ToolFailure("not_found", f"{company.ticker} has risk factors for only {years}")
    year_b = inp.year_b or years[0]
    year_a = inp.year_a or next((y for y in years if y < year_b), None)
    if year_a is None or year_a not in years or year_b not in years:
        raise ToolFailure("not_found", f"{company.ticker} has risk factors for {years}")
    rows = await fetch_dicts(
        ctx,
        select(risk_factors, filings.c.url, filings.c.filing_date)
        .join(filings, filings.c.accession_no == risk_factors.c.accession_no)
        .where(
            risk_factors.c.cik == company.cik,
            risk_factors.c.fiscal_year.in_((year_a, year_b)),
        )
        .order_by(risk_factors.c.ordinal),
    )
    a = [r for r in rows if r["fiscal_year"] == year_a]
    b = [r for r in rows if r["fiscal_year"] == year_b]
    vecs = await ctx.embedder.embed([_compare_text(r) for r in a] + [_compare_text(r) for r in b])
    pairs = match_risks(vecs[: len(a)], vecs[len(a) :])
    matched_a = {i for i, _, _ in pairs}
    matched_b = {j for _, j, _ in pairs}
    changed = [
        ChangedRisk(
            before=_ref(a[i]),
            after=_ref(b[j]),
            similarity=round(s, 3),
            category_changed=a[i]["category"] != b[j]["category"],
        )
        for i, j, s in pairs
        if s < UNCHANGED_SIMILARITY or a[i]["category"] != b[j]["category"]
    ]
    new = [b[j] for j in range(len(b)) if j not in matched_b]
    removed = [a[i] for i in range(len(a)) if i not in matched_a]

    out = CompareRiskFactorsOutput(
        ticker=company.ticker,
        year_a=year_a,
        year_b=year_b,
        new=[_ref(r) for r in new],
        removed=[_ref(r) for r in removed],
        changed=changed,
        unchanged_count=len(pairs) - len(changed),
        category_counts_a=_category_counts(a),
        category_counts_b=_category_counts(b),
    )
    by_id = {r["id"]: r for r in rows}
    cited = (
        [r["id"] for r in new + removed]
        + [c.after.id for c in changed]
        + [c.before.id for c in changed]
    )
    evidence = [_risk_evidence(company.ticker, by_id[i]) for i in dict.fromkeys(cited)]
    return Output(out, evidence)


# ---------------------------------------------------------------- get_8k_events
class Get8KEventsInput(BaseModel):
    ticker: str
    item_codes: list[str] | None = Field(
        None, description="8-K item codes, e.g. ['2.02'] earnings, ['5.02'] executive changes"
    )
    since: date | None = Field(None, description="Only events filed on/after this date")
    limit: int = Field(20, ge=1, le=50)


class EightKEvent(BaseModel):
    accession_no: str
    filing_date: date
    items: list[str]
    item_descriptions: list[str]
    url: str
    excerpt: str


class Get8KEventsOutput(BaseModel):
    ticker: str
    events: list[EightKEvent]
    total_matching: int


@tool("get_8k_events", Get8KEventsInput)
async def get_8k_events(ctx: ToolContext, inp: Get8KEventsInput) -> Output:
    """List a company's 8-K material events (newest first) with item codes and what they
    mean (e.g. 2.02 results of operations, 5.02 officer changes, 1.01 material agreements),
    optionally filtered by item codes and a start date."""
    (company,), _ = await resolve_tickers(ctx, [inp.ticker])
    stmt = (
        select(eight_k_events, filings.c.url)
        .join(filings, filings.c.accession_no == eight_k_events.c.accession_no)
        .where(eight_k_events.c.cik == company.cik)
    )
    if inp.item_codes:
        stmt = stmt.where(eight_k_events.c["items"].overlap(inp.item_codes))
    if inp.since:
        stmt = stmt.where(eight_k_events.c.filing_date >= inp.since)
    async with ctx.engine.connect() as conn:
        total = (await conn.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = await fetch_dicts(
        ctx, stmt.order_by(eight_k_events.c.filing_date.desc()).limit(inp.limit)
    )
    events = [
        EightKEvent(
            accession_no=r["accession_no"],
            filing_date=r["filing_date"],
            items=list(r["items"]),
            item_descriptions=describe_items(r["items"]),
            url=r["url"],
            excerpt=event_excerpt(r["body"], r["items"]),
        )
        for r in rows
    ]
    evidence = [
        Evidence(
            source_id=f"8k:{e.accession_no}",
            source_type="8k",
            ticker=company.ticker,
            title=f"{company.ticker} 8-K {e.filing_date}: " + "; ".join(e.item_descriptions),
            snippet=e.excerpt,
            url=e.url,
            form_type="8-K",
            filed_or_published=e.filing_date,
        )
        for e in events
    ]
    warn = [] if events else ["no matching 8-K events stored (8-Ks cover the last 12 months)"]
    return Output(
        Get8KEventsOutput(ticker=company.ticker, events=events, total_matching=total),
        evidence,
        warn,
    )
