from datetime import date
from pathlib import Path
from typing import Any

import httpx
import respx

from core.newsapi import NewsApiClient
from ingestion.edgar.client import EdgarClient
from ingestion.edgar.submissions import SUBMISSIONS_URL
from ingestion.refresh import RefreshPlan, plan_company, refresh_company

UA = "Test Person test@example.com"


class _Scalars:
    def __init__(self, values: set[str]) -> None:
        self.values = values

    def scalars(self) -> "_Scalars":
        return self

    def __iter__(self) -> Any:
        return iter(self.values)


class _Connection:
    def __init__(self, values: set[str]) -> None:
        self.values = values

    async def execute(self, _statement: Any) -> _Scalars:
        return _Scalars(self.values)

    async def __aenter__(self) -> "_Connection":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _Engine:
    def __init__(self, values: set[str]) -> None:
        self.values = values

    def connect(self) -> _Connection:
        return _Connection(self.values)


def _submissions(accessions: list[str]) -> dict[str, Any]:
    return {
        "filings": {
            "recent": {
                "accessionNumber": accessions,
                "form": ["8-K"] * len(accessions),
                "filingDate": ["2026-11-13"] * len(accessions),
                "reportDate": ["2026-11-13"] * len(accessions),
                "primaryDocument": [f"{a}.htm" for a in accessions],
                "items": ["2.02"] * len(accessions),
            }
        }
    }


@respx.mock
async def test_incremental_plan_skips_stored_8k(tmp_path: Path) -> None:
    route = respx.get(SUBMISSIONS_URL.format(cik=1)).mock(
        return_value=httpx.Response(200, json=_submissions(["old", "new"]))
    )
    edgar = EdgarClient(UA, tmp_path, http=httpx.AsyncClient())
    plan = await plan_company(
        _Engine({"old"}),
        edgar,
        None,
        "AAPL",
        1,
        today=date(2026, 11, 14),
        dry_run=True,
        news_cap=90,
    )
    assert route.call_count == 1
    assert [ref.accession_no for ref in plan.new_8ks] == ["new"]
    assert plan.sec_requests == 2
    await edgar.aclose()


@respx.mock
async def test_incremental_plan_respects_news_cap(tmp_path: Path) -> None:
    route = respx.get(SUBMISSIONS_URL.format(cik=1)).mock(
        return_value=httpx.Response(200, json=_submissions([]))
    )
    raw = tmp_path / "newsapi"
    for i in range(90):
        (raw / f"T{i}").mkdir(parents=True)
        (raw / f"T{i}" / "2026-11-14.json").write_text("{}", encoding="utf-8")
    edgar = EdgarClient(UA, tmp_path, http=httpx.AsyncClient())
    news = NewsApiClient("key", "https://newsapi.org/v2", tmp_path, http=httpx.AsyncClient())
    plan = await plan_company(
        _Engine(set()),
        edgar,
        news,
        "AAPL",
        1,
        today=date(2026, 11, 14),
        dry_run=True,
        news_cap=90,
    )
    assert route.call_count == 1
    assert plan.news_request_needed is False
    await edgar.aclose()
    await news.aclose()


async def test_dry_run_never_writes_risk_factors() -> None:
    plan = RefreshPlan("AAPL", 1, (), False, 1)

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("risk-factor storage must not be called")

    import ingestion.refresh as refresh_module

    original = refresh_module.pg.replace_risk_factors
    refresh_module.pg.replace_risk_factors = fail_if_called
    try:
        result = await refresh_company(
            _Engine(set()),
            None,  # type: ignore[arg-type]
            None,
            None,
            None,
            plan,
            company_name="Apple",
            today=date(2026, 11, 14),
            dry_run=True,
            news_cap=90,
        )
    finally:
        refresh_module.pg.replace_risk_factors = original
    assert result == {"ticker": "AAPL", "new_8ks": 0, "news_requests": 0, "sec_requests": 1}
