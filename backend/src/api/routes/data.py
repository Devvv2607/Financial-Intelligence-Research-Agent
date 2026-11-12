"""Read-only data endpoints: graph structure, companies, insights, recorded runs."""

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from api.routes.threads import runtime_of
from api.schemas import (
    CompanyDetail,
    CompanySummary,
    EightKItem,
    GraphEdge,
    GraphJSON,
    GraphNode,
    HeatmapCell,
    IngestionStats,
    MetricPoint,
    RiskCategory,
    RiskFactorItem,
    RiskHeatmap,
    RunDetail,
)
from core.vectorstore import FILING_CHUNK

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["data"])
RECENT_8KS = 10
# Display sector: the owner-reviewed universe sector first, then the LLM classification,
# then the SIC mapping.
SECTOR = "COALESCE(c.universe_sector, c.llm_sector, c.sic_sector)"
LATEST_RF = "SELECT cik, MAX(fiscal_year) AS fy FROM risk_factors GROUP BY cik"


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


@router.get("/graph", response_model=GraphJSON)
async def graph_structure(request: Request) -> GraphJSON:
    """Nodes and edges from `graph.get_graph(xray=1)`, specialist subgraphs expanded."""
    g = runtime_of(request).graph.get_graph(xray=1)
    nodes = [
        GraphNode(
            id=n.id,
            label=n.id.split(":")[-1],
            subgraph=n.id.split(":")[0] if ":" in n.id else None,
        )
        for n in g.nodes.values()
    ]
    edges = [
        GraphEdge(source=e.source, target=e.target, conditional=e.conditional) for e in g.edges
    ]
    return GraphJSON(nodes=nodes, edges=edges)


@router.get("/companies", response_model=list[CompanySummary])
async def list_companies(
    request: Request, q: str | None = None, sector: str | None = None
) -> list[CompanySummary]:
    """Ingested companies (those with filings), optionally filtered."""
    # Only module constants are interpolated; user input is always a bound parameter.
    sql = f"""
        SELECT c.ticker, c.name, {SECTOR} AS sector, c.sic_sector, c.llm_sector,
               c.sector_conflict, c.fiscal_year_end, l.fy AS latest_10k_year,
               (SELECT COUNT(*) FROM risk_factors r
                 WHERE r.cik = c.cik AND r.fiscal_year = l.fy) AS risk_factor_count
        FROM companies c
        LEFT JOIN ({LATEST_RF}) l ON l.cik = c.cik
        WHERE EXISTS (SELECT 1 FROM filings f WHERE f.cik = c.cik)
          AND (CAST(:q AS text) IS NULL
               OR c.ticker ILIKE :q_like OR c.name ILIKE :q_like)
          AND (CAST(:sector AS text) IS NULL OR {SECTOR} = :sector)
        ORDER BY c.ticker
    """  # noqa: S608
    params = {"q": q, "q_like": f"%{q}%" if q else None, "sector": sector}
    async with _engine(request).connect() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    return [CompanySummary.model_validate(dict(r)) for r in rows]


@router.get("/companies/{ticker}", response_model=CompanyDetail)
async def company_detail(ticker: str, request: Request) -> CompanyDetail:
    async with _engine(request).connect() as conn:
        c = (
            (
                await conn.execute(
                    text(f"SELECT c.*, {SECTOR} AS sector FROM companies c WHERE c.ticker = :t"),  # noqa: S608
                    {"t": ticker.upper()},
                )
            )
            .mappings()
            .one_or_none()
        )
        if c is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{ticker.upper()} is not covered.")
        cik = c["cik"]
        rfs = (
            (
                await conn.execute(
                    text(
                        "SELECT id, heading, summary, category, needs_review, fiscal_year "
                        "FROM risk_factors WHERE cik = :cik AND fiscal_year = "
                        "(SELECT MAX(fiscal_year) FROM risk_factors WHERE cik = :cik) "
                        "ORDER BY ordinal"
                    ),
                    {"cik": cik},
                )
            )
            .mappings()
            .all()
        )
        eight_ks = (
            (
                await conn.execute(
                    text(
                        "SELECT e.accession_no, e.filing_date, e.items, f.url "
                        "FROM eight_k_events e JOIN filings f USING (accession_no) "
                        "WHERE e.cik = :cik ORDER BY e.filing_date DESC LIMIT :n"
                    ),
                    {"cik": cik, "n": RECENT_8KS},
                )
            )
            .mappings()
            .all()
        )
        # One value per metric and fiscal year: the most recently filed (restatements win).
        facts = (
            (
                await conn.execute(
                    text(
                        "SELECT DISTINCT ON (metric, fiscal_year) metric, fiscal_year, value, "
                        "unit, period_end FROM financial_facts WHERE cik = :cik "
                        "ORDER BY metric, fiscal_year, filed DESC"
                    ),
                    {"cik": cik},
                )
            )
            .mappings()
            .all()
        )
    groups: dict[str, list[RiskFactorItem]] = defaultdict(list)
    for r in rfs:
        groups[r["category"] or "untagged"].append(RiskFactorItem.model_validate(dict(r)))
    breakdown = sorted(
        (RiskCategory(category=k, count=len(v), risk_factors=v) for k, v in groups.items()),
        key=lambda g: (-g.count, g.category),
    )
    metrics: dict[str, list[MetricPoint]] = defaultdict(list)
    for f in facts:
        metrics[f["metric"]].append(MetricPoint.model_validate(dict(f)))
    return CompanyDetail(
        ticker=c["ticker"],
        name=c["name"],
        cik=cik,
        sector=c["sector"],
        sic=c["sic"],
        sic_description=c["sic_description"],
        sic_sector=c["sic_sector"],
        llm_sector=c["llm_sector"],
        llm_sub_industry=c["llm_sub_industry"],
        llm_sector_rationale=c["llm_sector_rationale"],
        sector_conflict=c["sector_conflict"],
        fiscal_year_end=c["fiscal_year_end"],
        risk_fiscal_year=rfs[0]["fiscal_year"] if rfs else None,
        risk_breakdown=breakdown,
        recent_8ks=[EightKItem.model_validate(dict(e)) for e in eight_ks],
        metrics=dict(metrics),
    )


@router.get("/insights/risk-heatmap", response_model=RiskHeatmap)
async def risk_heatmap(request: Request) -> RiskHeatmap:
    """Sector x risk category counts over each company's latest 10-K, plus pipeline stats."""
    sql = f"""
        SELECT COALESCE({SECTOR}, 'Unknown') AS sector,
               COALESCE(r.category, 'untagged') AS category,
               COUNT(*) AS count, COUNT(DISTINCT r.cik) AS companies
        FROM risk_factors r
        JOIN ({LATEST_RF}) l ON l.cik = r.cik AND l.fy = r.fiscal_year
        JOIN companies c ON c.cik = r.cik
        GROUP BY 1, 2 ORDER BY 1, 2
    """  # noqa: S608
    stats_sql = """
        SELECT (SELECT COUNT(DISTINCT cik) FROM filings) AS companies,
               (SELECT COUNT(*) FROM filings) AS filings,
               (SELECT COUNT(*) FROM risk_factors) AS risk_factors,
               (SELECT COUNT(*) FROM eight_k_events) AS eight_k_events,
               (SELECT COUNT(*) FROM financial_facts) AS financial_facts,
               (SELECT COUNT(*) FROM news_articles) AS news_articles
    """
    async with _engine(request).connect() as conn:
        rows = (await conn.execute(text(sql))).mappings().all()
        stats = (await conn.execute(text(stats_sql))).mappings().one()
    cells = [HeatmapCell.model_validate(dict(r)) for r in rows]
    return RiskHeatmap(
        sectors=sorted({c.sector for c in cells}),
        categories=sorted({c.category for c in cells}),
        cells=cells,
        stats=IngestionStats(**dict(stats), chunks=await _chunk_count(request)),
    )


async def _chunk_count(request: Request) -> int | None:
    wv: Any = request.app.state.weaviate
    timeout = request.app.state.settings.readiness_timeout_s
    try:
        res = await asyncio.wait_for(
            wv.collections.get(FILING_CHUNK).aggregate.over_all(total_count=True), timeout
        )
        return int(res.total_count) if res.total_count is not None else None
    except Exception as exc:  # stats are secondary: show the rest without them
        logger.warning("chunk count unavailable", extra={"error": repr(exc)})
        return None


@router.get("/runs/{run_id}", response_model=RunDetail)
async def run_detail(run_id: uuid.UUID, request: Request) -> RunDetail:
    """A recorded run: plan, steps (with timings and tool calls), tokens per node."""
    async with _engine(request).connect() as conn:
        row = (
            (await conn.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": run_id}))
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    return RunDetail.model_validate(dict(row))
