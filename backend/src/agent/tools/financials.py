"""financials_agent tools: XBRL-derived annual metrics and a safe calculator."""

from collections import defaultdict
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from agent.tools.base import Evidence, Output, ToolContext, ToolFailure, tool
from agent.tools.calculator import CalculatorError, evaluate
from agent.tools.common import filing_index_url, missing_warning, resolve_tickers
from core.db.schema import financial_facts

Metric = Literal[
    "revenue", "net_income", "operating_income", "total_assets", "total_liabilities",
    "cash", "rd_expense", "eps_diluted",
]  # fmt: skip
ALL_METRICS: tuple[str, ...] = Metric.__args__  # type: ignore[attr-defined]


class GetFinancialMetricsInput(BaseModel):
    tickers: list[str] = Field(min_length=1)
    metrics: list[Metric] | None = Field(None, description="Default: all eight metrics")
    years: list[int] | None = Field(
        None, description="Fiscal years; default: each company's 3 most recent"
    )


class MetricValue(BaseModel):
    metric: str
    fiscal_year: int
    value: float
    unit: str
    period_start: date | None
    period_end: date
    concept: str


class CompanyMetrics(BaseModel):
    ticker: str
    fiscal_year_end: str | None = Field(description="MMDD; fiscal years end on different dates")
    values: list[MetricValue]
    missing: list[str] = Field(description="'metric FYyyyy' pairs the company does not report")


class GetFinancialMetricsOutput(BaseModel):
    companies: list[CompanyMetrics]


@tool("get_financial_metrics", GetFinancialMetricsInput)
async def get_financial_metrics(ctx: ToolContext, inp: GetFinancialMetricsInput) -> Output:
    """Annual financial metrics from 10-K XBRL data: revenue, net_income, operating_income,
    total_assets, total_liabilities, cash, rd_expense, eps_diluted (USD; EPS in USD/share).
    Each value is labelled by the year its fiscal period ends; fiscal year ends differ
    between companies (fiscal_year_end, MMDD) and must be stated when comparing."""
    known, missing = await resolve_tickers(ctx, inp.tickers)
    metrics = list(inp.metrics or ALL_METRICS)
    async with ctx.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(financial_facts)
                    .where(
                        financial_facts.c.cik.in_([c.cik for c in known]),
                        financial_facts.c.metric.in_(metrics),
                    )
                    .order_by(financial_facts.c.fiscal_year.desc())
                )
            )
            .mappings()
            .all()
        )
    by_cik: dict[int, list[dict[str, object]]] = defaultdict(list)
    for r in rows:
        by_cik[r["cik"]].append(dict(r))

    out: list[CompanyMetrics] = []
    evidence: list[Evidence] = []
    for c in known:
        facts = by_cik.get(c.cik, [])
        available = sorted({int(str(f["fiscal_year"])) for f in facts}, reverse=True)
        years = inp.years or available[:3]
        chosen = [f for f in facts if f["fiscal_year"] in years]
        have = {(f["metric"], f["fiscal_year"]) for f in chosen}
        values: list[MetricValue] = []
        for f in sorted(chosen, key=lambda f: (str(f["metric"]), -int(str(f["fiscal_year"])))):
            v = MetricValue(
                metric=str(f["metric"]),
                fiscal_year=int(str(f["fiscal_year"])),
                value=float(str(f["value"])),
                unit=str(f["unit"]),
                period_start=f["period_start"],
                period_end=f["period_end"],
                concept=str(f["concept"]),
            )
            values.append(v)
            evidence.append(
                Evidence(
                    source_id=f"fact:{c.cik}:{v.metric}:{v.fiscal_year}",
                    source_type="financial_fact",
                    ticker=c.ticker,
                    title=f"{c.ticker} {v.metric} FY{v.fiscal_year} (XBRL)",
                    snippet=(
                        f"{c.ticker} {v.metric} for the fiscal period ending {v.period_end}: "
                        f"{v.value:,.2f} {v.unit} (us-gaap:{v.concept}, 10-K XBRL)"
                    ),
                    url=filing_index_url(c.cik, str(f["accession_no"])),
                    form_type="10-K",
                    fiscal_year=v.fiscal_year,
                    filed_or_published=f["filed"],
                )
            )
        out.append(
            CompanyMetrics(
                ticker=c.ticker,
                fiscal_year_end=c.fiscal_year_end,
                values=values,
                missing=[f"{m} FY{y}" for m in metrics for y in years if (m, y) not in have],
            )
        )
    warnings = missing_warning(missing)
    if not evidence:
        warnings.append("no financial facts stored for these companies/years")
    return Output(GetFinancialMetricsOutput(companies=out), evidence, warnings)


class CalculateInput(BaseModel):
    expression: str = Field(
        description=(
            "Arithmetic using numbers and variable names: + - * / ** ( ), and functions "
            "abs, round, min, max, sqrt, pct_change(new, old), cagr(end, start, years)"
        )
    )
    variables: dict[str, float] = Field(
        default_factory=dict, description="Named inputs, e.g. {'rev_2025': 1.2e11}"
    )


class CalculateOutput(BaseModel):
    expression: str
    result: float


@tool("calculate", CalculateInput, timeout_s=2)
async def calculate(ctx: ToolContext, inp: CalculateInput) -> Output:
    """Exact arithmetic for growth rates, margins and ratios. Use it instead of doing math
    in your head, e.g. pct_change(rev_2025, rev_2024) or net_income / revenue * 100."""
    try:
        result = evaluate(inp.expression, inp.variables)
    except CalculatorError as exc:
        raise ToolFailure("invalid_input", str(exc)) from exc
    return Output(CalculateOutput(expression=inp.expression, result=result), [])
