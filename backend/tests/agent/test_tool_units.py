"""Phase 2 tool logic without any network or database: calculator, SQL guard, tool
envelope, risk matching, 8-K helpers, news staleness, and the text-to-SQL retry loop."""

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import BaseModel

from agent.tools import REGISTRY, call_tool
from agent.tools.base import Output, ToolContext, ToolFailure, tool
from agent.tools.calculator import CalculatorError, evaluate
from agent.tools.eight_k_items import describe_items, event_excerpt
from agent.tools.filings import match_risks
from agent.tools.news import STALE_AFTER, stale_companies
from agent.tools.screener import QueryResult, SQLDraft, text_to_sql
from agent.tools.sql_guard import ALLOWLIST, SQLRejected, validate_sql
from core.config import Settings
from core.llm import InvalidOutputError


# ---------------------------------------------------------------- calculator
@pytest.mark.parametrize(
    ("expr", "variables", "expected"),
    [
        ("1 + 2 * 3", {}, 7),
        ("(rev - prev) / prev * 100", {"rev": 150.0, "prev": 120.0}, 25),
        ("pct_change(150, 120)", {}, 25),
        ("pct_change(80, -100)", {}, 180),  # growth from a loss uses |old|
        ("round(cagr(200, 100, 2), 2)", {}, 41.42),
        ("max(1, 5, 3) - abs(-2) + sqrt(16)", {}, 7),
        ("-x ** 2", {"x": 3.0}, -9),
    ],
)
def test_calculator(expr: str, variables: dict[str, float], expected: float) -> None:
    assert evaluate(expr, variables) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("__import__('os').system('x')", "unsupported"),
        ("open('f')", "unsupported"),
        ("x.real", "unsupported"),
        ("y + 1", "unknown name"),
        ("1 / 0", "division by zero"),
        ("2 ** 1000", "exponent"),
        ("'a' * 3", "unsupported"),
        ("pct_change(1, 0)", "zero"),
        ("1 +", "syntax"),
        ("9" * 600, "longer than"),
    ],
)
def test_calculator_rejects(expr: str, message: str) -> None:
    with pytest.raises(CalculatorError, match=message):
        evaluate(expr, {"x": 1.0})


# ---------------------------------------------------------------- SQL guard
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE companies",
        "UPDATE companies SET name = 'x'",
        "DELETE FROM news_articles",
        "INSERT INTO companies (cik) VALUES (1)",
        "SELECT ticker FROM companies; DROP TABLE companies",
        "SELECT * FROM companies",
        "SELECT body FROM risk_factors",
        "SELECT r.body FROM risk_factors r",
        "WITH x AS (SELECT body FROM risk_factors) SELECT body FROM x",
        "SELECT text FROM filing_sections",
        "SELECT usename FROM pg_user",
        "SELECT tablename FROM pg_catalog.pg_tables",
        "SELECT pg_sleep(10)",
        "SELECT ticker INTO stolen FROM companies",
        "SELECT z.ticker FROM companies c",
        "COPY companies TO '/tmp/x'",
        "not even sql",
    ],
)
def test_sql_guard_rejects(sql: str) -> None:
    with pytest.raises(SQLRejected):
        validate_sql(sql)


def test_sql_guard_accepts_and_caps_limit() -> None:
    sql = (
        "WITH p AS (SELECT pr.cik, pr.value AS pv FROM financial_facts pr "
        "WHERE pr.metric = 'revenue') "
        "SELECT c.ticker, pr.pv FROM companies c JOIN p pr ON pr.cik = c.cik "
        "WHERE EXISTS (SELECT 1 FROM risk_factors r WHERE r.cik = c.cik "
        "AND r.fiscal_year = (SELECT MAX(r2.fiscal_year) FROM risk_factors r2 "
        "WHERE r2.cik = c.cik)) ORDER BY pr.pv DESC LIMIT 5000"
    )
    v = validate_sql(sql)
    assert v.limit_applied and v.sql.endswith("LIMIT 100")
    assert v.tables == ("companies", "financial_facts", "risk_factors")
    assert not validate_sql("SELECT COUNT(*) FROM companies LIMIT 10").limit_applied


def test_readonly_role_grants_match_the_allowlist() -> None:
    """The migration freezes the column grants; they must equal the validator's allowlist."""
    path = next(Path("backend/alembic/versions").glob("*read_only_role*.py"))
    spec = importlib.util.spec_from_file_location("readonly_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {t: tuple(cols) for t, cols in ALLOWLIST.items()} == module.GRANTS


# ---------------------------------------------------------------- tool envelope
class _In(BaseModel):
    n: int


class _Out(BaseModel):
    n: int


@tool("_test_ok", _In)
async def _ok(ctx: ToolContext, inp: _In) -> Output:
    """Test tool."""
    return Output(_Out(n=inp.n * 2), [])


@tool("_test_fail", _In)
async def _fail(ctx: ToolContext, inp: _In) -> Output:
    """Test tool."""
    raise ToolFailure("not_found", "nothing here")


@tool("_test_crash", _In)
async def _crash(ctx: ToolContext, inp: _In) -> Output:
    """Test tool."""
    raise RuntimeError("boom")


@tool("_test_slow", _In, timeout_s=0.05)
async def _slow(ctx: ToolContext, inp: _In) -> Output:
    """Test tool."""
    await asyncio.sleep(1)
    return Output(_Out(n=0), [])


CTX = ToolContext(
    settings=Settings(_env_file=None),
    engine=None,
    weaviate=None,  # type: ignore[arg-type]
    embedder=None,
    llm=None,
)


@pytest.mark.parametrize(
    ("name", "args", "ok", "code"),
    [
        ("_test_ok", {"n": 2}, True, None),
        ("_test_ok", {"n": "x"}, False, "invalid_input"),
        ("_test_fail", {"n": 1}, False, "not_found"),
        ("_test_crash", {"n": 1}, False, "internal"),
        ("_test_slow", {"n": 1}, False, "timeout"),
        ("no_such_tool", {}, False, "invalid_input"),
    ],
)
async def test_call_tool_never_raises(
    name: str, args: dict[str, Any], ok: bool, code: str | None
) -> None:
    result = await call_tool(CTX, name, args)
    assert result.ok is ok
    assert (result.error.code if result.error else None) == code
    if ok:
        assert result.data == {"n": 4}


def test_all_spec_tools_registered_with_descriptions() -> None:
    expected = {
        "search_filings",
        "get_risk_factors",
        "compare_risk_factors",
        "get_8k_events",
        "get_financial_metrics",
        "calculate",
        "search_news",
        "query_database",
    }
    assert expected <= set(REGISTRY)
    assert all(len(REGISTRY[t].description) > 60 for t in expected)


# ---------------------------------------------------------------- logic helpers
def test_match_risks_is_one_to_one_and_thresholded() -> None:
    a = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)
    v = np.array([0.95, 0.31, 0], dtype=np.float32)
    b = np.stack(
        [
            v / np.linalg.norm(v),
            np.array([0, 1, 0], np.float32),
            np.array([0.6, 0.0, 0.8], np.float32),
        ]
    )
    pairs = {(i, j) for i, j, _ in match_risks(a, b)}
    assert pairs == {(0, 0), (1, 1)}  # a[2] vs b[2] is 0.8 < 0.90: removed + new
    assert match_risks(a[:0], b) == []


def test_8k_helpers() -> None:
    assert describe_items(["2.02", "9.99"]) == [
        "2.02 Results of operations and financial condition",
        "9.99 Unknown item",
    ]
    body = "UNITED STATES ... cover page ... Item 9.01 Exhibits. Item 2.02 Results. Revenue rose."
    assert event_excerpt(body, ["2.02", "9.01"]).startswith("Item 2.02 Results")


def test_news_staleness() -> None:
    now = datetime(2026, 11, 4, tzinfo=UTC)
    latest = {1: now - timedelta(hours=5), 2: now - STALE_AFTER - timedelta(minutes=1), 3: None}
    assert stale_companies(latest, now) == [2, 3]


# ---------------------------------------------------------------- text-to-SQL loop
class ScriptedSQL:
    def __init__(self, drafts: list[str]) -> None:
        self.drafts = drafts
        self.calls: list[list[dict[str, str]]] = []

    async def structured(
        self, slot: str, schema: type, messages: list[dict[str, str]], **kw: Any
    ) -> Any:
        self.calls.append(messages)
        return SQLDraft(sql=self.drafts[len(self.calls) - 1], explanation="x")


def _ctx(llm: Any) -> ToolContext:
    return ToolContext(
        settings=Settings(_env_file=None),
        engine=None,
        weaviate=None,  # type: ignore[arg-type]
        embedder=None,
        llm=llm,
    )


async def test_text_to_sql_retries_once_with_validator_feedback() -> None:
    llm = ScriptedSQL(["SELECT * FROM companies", "SELECT ticker FROM companies"])
    executed: list[str] = []

    async def execute(sql: str) -> QueryResult:
        executed.append(sql)
        return QueryResult(columns=["ticker"], rows=[["AAPL"]])

    _, _, result, attempts = await text_to_sql(_ctx(llm), "list tickers", execute)
    assert attempts == 2 and result.rows == [["AAPL"]]
    assert executed == ["SELECT ticker FROM companies LIMIT 100"]  # bad SQL never ran
    feedback = llm.calls[1][-1]["content"]
    assert "SELECT * is not allowed" in feedback
    assert "Schema" in llm.calls[0][0]["content"] and "risk_factors" in llm.calls[0][0]["content"]


async def test_text_to_sql_gives_up_after_two_attempts() -> None:
    llm = ScriptedSQL(["DROP TABLE companies", "DELETE FROM companies"])

    async def execute(sql: str) -> QueryResult:
        raise AssertionError("must not execute rejected SQL")

    with pytest.raises(ToolFailure, match="after 2 attempts") as info:
        await text_to_sql(_ctx(llm), "wipe it", execute)
    assert "DELETE FROM companies" in str(info.value)  # last SQL is shown for transparency


async def test_text_to_sql_invalid_model_output_is_upstream_error() -> None:
    class Broken:
        async def structured(self, *a: Any, **kw: Any) -> Any:
            raise InvalidOutputError("bad", "{}")

    async def execute(sql: str) -> QueryResult:
        raise AssertionError

    with pytest.raises(ToolFailure) as info:
        await text_to_sql(_ctx(Broken()), "q", execute)
    assert info.value.code == "upstream"
