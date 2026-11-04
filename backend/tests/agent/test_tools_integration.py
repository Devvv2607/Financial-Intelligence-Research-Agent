"""Tools against the real local stores. Skipped automatically when the stack is down.

Every test discovers its subject from the database (the first ticker with data) instead
of assuming specific companies, so this works on the 10-company sample and on the full
universe alike. No LLM is used: query_database's SQL execution is tested directly.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from agent.tools import ToolContext, call_tool
from agent.tools.screener import run_readonly
from agent.tools.sql_guard import validate_sql

pytestmark = pytest.mark.integration


async def _ticker_with(ctx: ToolContext, table: str) -> str:
    async with ctx.engine.connect() as conn:
        return str(
            (
                await conn.execute(
                    text(
                        f"SELECT c.ticker FROM companies c WHERE EXISTS "
                        f"(SELECT 1 FROM {table} t WHERE t.cik = c.cik) ORDER BY 1 LIMIT 1"
                    )
                )
            ).scalar_one()
        )


async def test_unknown_ticker_lists_available_companies(live_ctx: ToolContext) -> None:
    r = await call_tool(live_ctx, "get_risk_factors", {"ticker": "ZZZZ"})
    assert not r.ok and r.error is not None and r.error.code == "not_found"
    assert "Available tickers" in r.error.message


async def test_get_risk_factors_and_evidence(live_ctx: ToolContext) -> None:
    ticker = await _ticker_with(live_ctx, "risk_factors")
    r = await call_tool(live_ctx, "get_risk_factors", {"ticker": ticker})
    assert r.ok, r.error
    assert r.data["total"] >= 1 and r.data["risk_factors"]
    assert all(e.source_id.startswith("rf:") and e.url for e in r.evidence)
    headings = [rf["heading"].lower() for rf in r.data["risk_factors"]]
    assert "table of contents" not in headings  # regression: navigation links


async def test_compare_risk_factors_partitions_every_risk(live_ctx: ToolContext) -> None:
    async with live_ctx.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT c.ticker FROM companies c JOIN risk_factors r ON r.cik = c.cik "
                    "GROUP BY c.ticker HAVING COUNT(DISTINCT r.fiscal_year) >= 2 ORDER BY 1 LIMIT 1"
                )
            )
        ).scalar_one_or_none()
    if row is None:
        pytest.skip("no company with two 10-Ks of risk factors")
    r = await call_tool(live_ctx, "compare_risk_factors", {"ticker": row})
    assert r.ok, r.error
    d = r.data
    n_a, n_b = sum(d["category_counts_a"].values()), sum(d["category_counts_b"].values())
    c = d["counts"]
    matched = c["changed"] + c["unchanged"]
    assert matched + c["removed"] == n_a and matched + c["new"] == n_b
    assert c["changed"] == len(d["changed"]) and c["unchanged"] == len(d["unchanged"])
    assert all(x["text_similarity"] >= 0.95 for x in d["unchanged"])
    assert r.evidence[0].source_id == f"compare:{row}:{d['year_a']}:{d['year_b']}"
    # Years passed newest-first must not flip "new" and "removed" (demo 2 regression).
    flipped = await call_tool(
        live_ctx,
        "compare_risk_factors",
        {"ticker": row, "year_a": d["year_b"], "year_b": d["year_a"]},
    )
    assert flipped.ok and flipped.data["counts"] == c
    assert (flipped.data["year_a"], flipped.data["year_b"]) == (d["year_a"], d["year_b"])


async def test_financial_metrics_and_8k_events(live_ctx: ToolContext) -> None:
    ticker = await _ticker_with(live_ctx, "financial_facts")
    r = await call_tool(live_ctx, "get_financial_metrics", {"tickers": [ticker, "ZZZZ"]})
    assert r.ok and r.data["companies"][0]["values"]
    assert any("ZZZZ" in w for w in r.warnings)  # partial answer + warning, not a failure
    ticker_8k = await _ticker_with(live_ctx, "eight_k_events")
    r = await call_tool(live_ctx, "get_8k_events", {"ticker": ticker_8k, "limit": 3})
    assert r.ok and r.data["events"]
    assert all(e["item_descriptions"] for e in r.data["events"])


async def test_search_filings_hybrid_with_filters(live_ctx: ToolContext) -> None:
    ticker = await _ticker_with(live_ctx, "filings")
    r = await call_tool(
        live_ctx,
        "search_filings",
        {
            "query": "competition and pricing pressure",
            "tickers": [ticker],
            "sections": ["item_1a"],
            "limit": 3,
        },
    )
    assert r.ok, r.error
    assert r.data["hits"] and all(h["ticker"] == ticker for h in r.data["hits"])
    assert all(h["section"] == "item_1a" for h in r.data["hits"])


async def test_readonly_role_blocks_writes_and_hidden_columns(live_ctx: ToolContext) -> None:
    """Layer 2 holds even if the validator (layer 1) were bypassed."""
    ok = await run_readonly(live_ctx.engine, validate_sql("SELECT ticker FROM companies").sql)
    assert ok.columns == ["ticker"] and ok.rows
    for sql in ("DELETE FROM news_articles WHERE false", "SELECT body FROM risk_factors LIMIT 1"):
        with pytest.raises(DBAPIError):
            await run_readonly(live_ctx.engine, sql)
