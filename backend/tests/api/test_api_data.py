"""Data endpoints against the local stack and whatever companies are ingested.

Skipped when Postgres isn't reachable or nothing is ingested (CI). No agent runtime is
built (no Groq, no embedding model): these routes only read the stores.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text

from api.main import create_app
from core.config import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(), runtime_factory=None)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        try:
            async with app.state.engine.connect() as conn:
                n = (await conn.execute(text("SELECT count(*) FROM risk_factors"))).scalar_one()
        except Exception as exc:
            pytest.skip(f"local stack not reachable: {type(exc).__name__}")
        if not n:
            pytest.skip("no ingested companies")
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_companies_list_filters_and_detail(client: httpx.AsyncClient) -> None:
    companies = (await client.get("/api/companies")).json()
    assert companies and all(c["latest_10k_year"] for c in companies)
    first = companies[0]
    assert first["risk_factor_count"] > 0

    by_q = (await client.get("/api/companies", params={"q": first["ticker"].lower()})).json()
    assert first["ticker"] in [c["ticker"] for c in by_q]
    by_sector = (await client.get("/api/companies", params={"sector": first["sector"]})).json()
    assert by_sector and {c["sector"] for c in by_sector} == {first["sector"]}

    d = (await client.get(f"/api/companies/{first['ticker'].lower()}")).json()
    assert d["ticker"] == first["ticker"]
    assert d["risk_fiscal_year"] == first["latest_10k_year"]
    assert sum(g["count"] for g in d["risk_breakdown"]) == first["risk_factor_count"]
    counts = [g["count"] for g in d["risk_breakdown"]]
    assert counts == sorted(counts, reverse=True)  # biggest category first
    dates = [e["filing_date"] for e in d["recent_8ks"]]
    assert dates == sorted(dates, reverse=True) and len(dates) <= 10
    for points in d["metrics"].values():
        years = [p["fiscal_year"] for p in points]
        assert years == sorted(set(years))  # one value per year, oldest first (sparklines)


async def test_uncovered_company_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/companies/ZZZZ")
    assert r.status_code == 404 and "not covered" in r.json()["detail"]


async def test_risk_heatmap_matches_company_counts(client: httpx.AsyncClient) -> None:
    h = (await client.get("/api/insights/risk-heatmap")).json()
    companies = (await client.get("/api/companies")).json()
    assert sum(c["count"] for c in h["cells"]) == sum(c["risk_factor_count"] for c in companies)
    assert set(h["sectors"]) == {c["sector"] for c in h["cells"]}
    stats = h["stats"]
    assert stats["companies"] == len(companies)
    assert stats["risk_factors"] >= sum(c["count"] for c in h["cells"])  # all years >= latest
    assert stats["chunks"] is None or stats["chunks"] > 0


async def test_run_detail_and_404(client: httpx.AsyncClient) -> None:
    transport = client._transport
    app = transport.app  # type: ignore[attr-defined]
    async with app.state.engine.connect() as conn:
        run_id = (
            await conn.execute(text("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1"))
        ).scalar_one_or_none()
    if run_id is None:
        pytest.skip("no recorded agent runs")
    r = (await client.get(f"/api/runs/{run_id}")).json()
    assert r["id"] == str(run_id) and r["query"]
    assert (await client.get("/api/runs/00000000-0000-0000-0000-000000000000")).status_code == 404
