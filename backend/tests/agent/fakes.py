"""Scripted stand-ins for the LLM, the tools and the company list (no network)."""

from datetime import date
from typing import Any

from agent.deps import AgentDeps
from agent.scripted import ScriptedLLM, ScriptedTools, failing, ok
from agent.tools.base import Evidence
from agent.tools.common import Company

COMPANIES = [
    Company(1, "NVDA", "NVIDIA CORP", "Information Technology", "0125"),
    Company(2, "AMD", "ADVANCED MICRO DEVICES INC", "Information Technology", "1227"),
    Company(3, "INTC", "INTEL CORP", "Information Technology", "1227"),
    Company(4, "AAPL", "Apple Inc.", "Information Technology", "0926"),
    Company(5, "JPM", "JPMORGAN CHASE & CO", "Financials", "1231"),
    Company(6, "JNJ", "JOHNSON & JOHNSON", "Health Care", "1228"),
    Company(7, "JCI", "Johnson Controls International plc", "Industrials", "0930"),
    Company(8, "TSLA", "Tesla, Inc.", "Consumer Discretionary", "1231"),
]

# The scripted LLM/tools ship in the package (the API's scripted E2E runtime uses them);
# the tests use them under their historical names.
FakeLLM = ScriptedLLM
FakeTools = ScriptedTools
__all__ = ["FakeLLM", "FakeTools", "failing", "ok"]  # re-exported for the tests


def ev(source_id: str, ticker: str = "NVDA") -> Evidence:
    return Evidence(
        source_id=source_id,
        source_type="risk_factor",
        ticker=ticker,
        title=f"{ticker} source {source_id}",
        snippet="supporting text",
        url=f"https://www.sec.gov/{source_id}",
    )


def make_deps(
    llm: FakeLLM, tools: FakeTools, recorded: list[dict[str, Any]] | None = None
) -> AgentDeps:
    async def companies() -> list[Company]:
        return list(COMPANIES)

    async def record(run: dict[str, Any]) -> None:
        if recorded is not None:
            recorded.append(run)

    return AgentDeps(
        llm=llm,
        call_tool=tools,
        list_companies=companies,
        today=lambda: date(2026, 11, 4),
        record_run=record,
    )


def analysis(query: str, mentions: list[str] | None = None, **kw: Any) -> dict[str, Any]:
    base = {
        "standalone_query": query,
        "advice_intent": False,
        "needs_clarification": False,
        "clarification_question": "",
        "clarification_options": [],
        "company_mentions": mentions or [],
    }
    return base | kw


def step(
    id_: str,
    agent: str,
    goal: str = "do it",
    entities: list[str] | None = None,
    depends_on: list[str] | None = None,
    tool_calls: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "goal": goal,
        "agent": agent,
        "entities": entities or [],
        "depends_on": depends_on or [],
        "tool_calls": [{"tool": t, "args_json": a} for t, a in (tool_calls or [])],
    }


OK_CRITIQUE = {"verdict": "ok", "gaps": [], "unanswerable": [], "notes": "fine"}
