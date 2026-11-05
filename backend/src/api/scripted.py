"""Scripted agent runtime: the real graph with canned LLM answers and canned tools.

AGENT_RUNTIME=scripted (refused when APP_ENV=production). For zero-token browser E2E:
the graph, API, SSE, checkpointer semantics and citation check are the real ones; only
the LLM and the tools are replaced. Every source it returns is visibly labelled
"(scripted)" and points to example.com, so it can't be mistaken for filing data.

Scenarios, picked from the question:
- "...NVIDIA and AMD..."      4 parallel direct steps, a comparison table, one claim no
                               source supports (the citation check marks it [unverified])
- "...Apple...change..."      critic finds a gap -> replan round 2 with an agentic step
- "...the bank..."            analyzer clarification -> resume with "JPMorgan Chase (JPM)"
- "...Johnson..."             entity-resolver clarification (JNJ vs JCI) -> resume
- "Should I buy/sell ..."     guardrail refusal, no LLM call at all
- anything else               the planner finds no steps -> a plain error answer
"""

import re
import zlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from numpy.typing import NDArray
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.checkpoint import memory_saver
from agent.deps import AgentDeps
from agent.scripted import ScriptedLLM, ScriptedTools, ToolHandler
from agent.tools.base import Evidence, ToolResult
from agent.tools.common import Company
from api.runtime import AgentRuntime, make_runtime
from core.config import Settings
from core.threads import MemoryThreadStore

TOOL_DELAY_S = 0.3  # slow enough for the UI to show running steps, fast enough for E2E
TOKEN_DELAY_S = 0.015

COMPANIES = [
    Company(1, "NVDA", "NVIDIA CORP", "Information Technology", "0125"),
    Company(2, "AMD", "ADVANCED MICRO DEVICES INC", "Information Technology", "1227"),
    Company(3, "AAPL", "Apple Inc.", "Information Technology", "0926"),
    Company(4, "JPM", "JPMORGAN CHASE & CO", "Financials", "1231"),
    Company(5, "JNJ", "JOHNSON & JOHNSON", "Health Care", "1228"),
    Company(6, "JCI", "Johnson Controls International plc", "Industrials", "0930"),
    Company(7, "TSLA", "Tesla, Inc.", "Consumer Discretionary", "1231"),
]

RISKS: dict[str, list[tuple[str, str]]] = {
    "NVDA": [
        (
            "Export controls restrict sales of advanced chips to China",
            "Export controls restrict sales of advanced chips to China and other regions.",
        ),
        (
            "Competition could reduce market share",
            "Competition could reduce market share in data center accelerators.",
        ),
        (
            "Reliance on third-party foundries",
            "Reliance on third-party foundries limits control over supply and lead times.",
        ),
    ],
    "AMD": [
        (
            "Highly competitive and rapidly evolving markets",
            "Highly competitive and rapidly evolving markets for processors and accelerators.",
        ),
        (
            "Reliance on third-party manufacturing",
            "Reliance on third-party manufacturing for wafers, assembly and test.",
        ),
        (
            "Cyclical semiconductor industry",
            "The cyclical semiconductor industry has had severe downturns.",
        ),
    ],
    "JPM": [
        (
            "Significant legal and regulatory proceedings",
            "Significant legal and regulatory proceedings could result in penalties.",
        ),
        (
            "Interest rate and credit spread changes",
            "Interest rate and credit spread changes affect earnings and liquidity.",
        ),
    ],
    "JNJ": [
        ("Product liability litigation", "Product liability litigation over talc products."),
        ("Drug pricing regulation", "Drug pricing regulation could reduce revenue."),
    ],
}


def _ev(source_id: str, kind: Any, ticker: str, title: str, snippet: str, **kw: Any) -> Evidence:
    return Evidence(
        source_id=f"scripted:{source_id}",
        source_type=kind,
        ticker=ticker,
        title=f"{title} (scripted)",
        snippet=snippet,
        url=f"https://example.com/scripted/{source_id}",
        **kw,
    )


def _risks(ticker: str) -> list[Evidence]:
    return [
        _ev(
            f"rf-{ticker}-{i}",
            "risk_factor",
            ticker,
            f"{ticker} 10-K risk factor: {h}",
            s,
            form_type="10-K",
        )
        for i, (h, s) in enumerate(RISKS.get(ticker, []))
    ]


def _news(ticker: str) -> list[Evidence]:
    items = {
        "NVDA": [
            (
                "NVIDIA tells cloud clients to limit China exposure",
                "NVIDIA tells cloud clients to limit China exposure under export controls.",
            )
        ],
        "AMD": [
            (
                "AMD faces new competition in AI accelerators",
                "AMD faces new competition in AI accelerators from custom chips.",
            )
        ],
        "AAPL": [
            (
                "Regulators examine AI features in Apple devices",
                "Regulators examine AI features in Apple devices and data use.",
            ),
            ("Apple expands AI investment", "Apple expands AI investment in compute capacity."),
        ],
    }.get(ticker, [])
    return [_ev(f"news-{ticker}-{i}", "news", ticker, t, s) for i, (t, s) in enumerate(items)]


async def _tool(name: str, args: dict[str, Any]) -> ToolResult:
    ticker = str(args.get("ticker") or (args.get("tickers") or ["?"])[0]).upper()
    if name == "get_risk_factors":
        ev = _risks(ticker)
    elif name == "search_news":
        ev = _news(ticker)
    elif name == "compare_risk_factors":
        ev = [
            _ev(
                "cmp-AAPL",
                "risk_factor",
                "AAPL",
                "AAPL risk factors, FY2025 vs FY2026 10-K",
                "Comparing the FY2025 and FY2026 10-K: 1 new, 0 removed, 20 materially "
                "changed, 7 unchanged.",
                form_type="10-K",
            ),
            _ev(
                "rf-AAPL-ai",
                "risk_factor",
                "AAPL",
                "AAPL 10-K risk factor: Artificial intelligence may cause reputational harm",
                "Artificial intelligence may cause reputational harm and regulatory action.",
                form_type="10-K",
            ),
        ]
    elif name == "get_8k_events":
        ev = [
            _ev(
                f"8k-{ticker}-0",
                "8k",
                ticker,
                f"{ticker} 8-K: Results of operations",
                "Item 2.02 Results of operations and financial condition.",
                form_type="8-K",
            )
        ]
    else:
        ev = []
    return ToolResult(tool=name, ok=True, data={"items": len(ev)}, evidence=ev)


# ---------------------------------------------------------------- scenario routing
_Q_RE = re.compile(r"^(?:Question|Overall question \(for context\)): (.+)$", re.MULTILINE)


def _text(messages: Sequence[Any]) -> str:
    parts = [m.get("content", "") if isinstance(m, dict) else m.content for m in messages]
    return "\n".join(str(p) for p in parts)


def _scenario(messages: Sequence[Any]) -> str:
    m = _Q_RE.search(_text(messages))
    q = (m.group(1) if m else "").lower()
    if "nvidia" in q and "amd" in q:
        return "compare"
    if "apple" in q and "change" in q:
        return "apple"
    if "jpmorgan" in q or "bank" in q:
        return "bank"
    if "johnson" in q:
        return "johnson"
    return "unknown"


def _analysis(q: str, mentions: list[str], **kw: Any) -> dict[str, Any]:
    return {
        "standalone_query": q,
        "advice_intent": False,
        "needs_clarification": False,
        "clarification_question": "",
        "clarification_options": [],
        "company_mentions": mentions,
    } | kw


def _analyzer(messages: Sequence[Any]) -> dict[str, Any]:
    text, s = _text(messages), _scenario(messages)
    if s == "compare":
        return _analysis(
            "Compare NVIDIA's and AMD's top 10-K risk factors with recent news.", ["NVIDIA", "AMD"]
        )
    if s == "apple":
        return _analysis(
            "How did Apple's risk factors change between its last two 10-Ks?", ["Apple"]
        )
    if s == "bank":
        if "The user answered" in text:
            return _analysis("How risky is JPMorgan Chase?", ["JPMorgan Chase"])
        return _analysis(
            "How risky is the bank?",
            [],
            needs_clarification=True,
            clarification_question="Which bank do you mean?",
            clarification_options=["JPMorgan Chase (JPM)", "Another bank (not covered)"],
        )
    if s == "johnson":
        return _analysis("How risky is Johnson & Johnson?", ["Johnson & Johnson"])
    return _analysis("(scripted mode: unknown question)", [])


def _step(
    id_: str, agent: str, goal: str, calls: list[tuple[str, str]], **kw: Any
) -> dict[str, Any]:
    return {
        "id": id_,
        "goal": goal,
        "agent": agent,
        "entities": kw.get("entities", []),
        "depends_on": kw.get("depends_on", []),
        "tool_calls": [{"tool": t, "args_json": a} for t, a in calls],
    }


def _planner(messages: Sequence[Any]) -> dict[str, Any]:
    text, s = _text(messages), _scenario(messages)
    if s == "compare":
        return {
            "steps": [
                _step(
                    "a",
                    "filings",
                    "NVIDIA's latest 10-K risk factors",
                    [("get_risk_factors", '{"ticker": "NVDA"}')],
                    entities=["NVDA"],
                ),
                _step(
                    "b",
                    "filings",
                    "AMD's latest 10-K risk factors",
                    [("get_risk_factors", '{"ticker": "AMD"}')],
                    entities=["AMD"],
                ),
                _step(
                    "c",
                    "news",
                    "Recent NVIDIA news",
                    [("search_news", '{"query": "NVIDIA risks", "tickers": ["NVDA"]}')],
                    entities=["NVDA"],
                ),
                _step(
                    "d",
                    "news",
                    "Recent AMD news",
                    [("search_news", '{"query": "AMD risks", "tickers": ["AMD"]}')],
                    entities=["AMD"],
                ),
            ]
        }
    if s == "apple":
        if "Gaps to fill" in text:  # replan round: an agentic step (the LLM picks the tool)
            return {
                "steps": [
                    _step("n", "news", "Recent news on Apple's AI risk", [], entities=["AAPL"])
                ]
            }
        return {
            "steps": [
                _step(
                    "a",
                    "filings",
                    "Compare Apple's last two 10-K risk factors",
                    [("compare_risk_factors", '{"ticker": "AAPL"}')],
                    entities=["AAPL"],
                )
            ]
        }
    if s == "bank":
        return {
            "steps": [
                _step(
                    "a",
                    "filings",
                    "JPMorgan's latest 10-K risk factors",
                    [("get_risk_factors", '{"ticker": "JPM"}')],
                    entities=["JPM"],
                ),
                _step(
                    "b",
                    "filings",
                    "JPMorgan's recent 8-K events",
                    [("get_8k_events", '{"ticker": "JPM"}')],
                    entities=["JPM"],
                ),
            ]
        }
    if s == "johnson":
        return {
            "steps": [
                _step(
                    "a",
                    "filings",
                    "Johnson & Johnson's 10-K risk factors",
                    [("get_risk_factors", '{"ticker": "JNJ"}')],
                    entities=["JNJ"],
                )
            ]
        }
    return {"steps": []}


def _critic(messages: Sequence[Any]) -> dict[str, Any]:
    if _scenario(messages) == "apple" and "r2s1" not in _text(messages):
        return {
            "verdict": "gaps",
            "gaps": ["Recent news on Apple's AI risk"],
            "unanswerable": [],
            "notes": "News would show whether the new risk is live.",
        }
    return {"verdict": "ok", "gaps": [], "unanswerable": [], "notes": "Enough to answer."}


def _cite(text: str, keyword: str) -> str:
    """The [n] of the numbered source whose title contains `keyword` (empty if none)."""
    m = re.search(rf"^\[(\d+)\] [^\n]*{re.escape(keyword)}", text, re.MULTILINE | re.IGNORECASE)
    return f"[{m.group(1)}]" if m else ""


def _synthesizer(messages: Sequence[BaseMessage]) -> str:
    text, s = _text(messages), _scenario(messages)

    def c(keyword: str) -> str:
        return _cite(text, keyword)

    if s == "compare":
        return (
            "**NVIDIA and AMD both flag competition and reliance on outside manufacturing; "
            "only NVIDIA's export-control risk has direct news support.**\n\n"
            "| Risk | NVIDIA | AMD |\n|---|---|---|\n"
            f"| Competition | Competition could reduce market share {c('Competition could')} "
            f"| Highly competitive and rapidly evolving markets {c('Highly competitive')} |\n"
            f"| Manufacturing | Reliance on third-party foundries {c('foundries')} "
            f"| Reliance on third-party manufacturing {c('third-party manufacturing')} |\n\n"
            "NVIDIA tells cloud clients to limit China exposure under export controls "
            f"{c('limit China')}. Its 10-K says export controls restrict sales of advanced "
            f"chips to China {c('Export controls')}. "
            # Deliberately unsupported: the citation check must mark it [unverified], so the
            # UI's "Verifying citations..." swap is exercised end to end.
            "Quarterly gaming revenue doubled on record console demand [1]."
        )
    if s == "apple":
        return (
            "Comparing the FY2025 and FY2026 10-K risk factors: 1 new, 0 removed, 20 "
            f"materially changed and 7 unchanged {c('FY2025 vs FY2026')}. "
            "The new risk: artificial intelligence may cause "
            f"reputational harm and regulatory action {c('Artificial intelligence')}. "
            f"Regulators examine AI features in Apple devices {c('Regulators examine')}."
        )
    if s == "bank":
        return (
            "JPMorgan Chase discloses significant legal and regulatory proceedings that "
            f"could result in penalties {c('legal and regulatory')}, and interest rate and "
            f"credit spread changes that affect earnings {c('Interest rate')}. Its latest "
            f"8-K reported results of operations {c('Results of operations')}."
        )
    if s == "johnson":
        return (
            "Johnson & Johnson discloses product liability litigation over talc products "
            f"{c('Product liability')} and drug pricing regulation that could reduce "
            f"revenue {c('Drug pricing')}."
        )
    return "Scripted mode only knows the E2E demo questions."


def _specialist(messages: Sequence[Any]) -> AIMessage:
    if not any(isinstance(m, ToolMessage) for m in messages):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_news",
                    "args": {
                        "query": "Apple artificial intelligence regulation",
                        "tickers": ["AAPL"],
                    },
                    "id": "call_scripted_1",
                    "type": "tool_call",
                }
            ],
        )
    return AIMessage(content="Two recent headlines cover Apple's AI risk.")


async def bag_of_words_embed(texts: list[str]) -> NDArray[np.float32]:
    """Deterministic stand-in for the embedding model (citation check in scripted mode)."""
    out = np.zeros((len(texts), 512), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            out[i, zlib.crc32(w.encode()) % 512] += 1
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return (out / np.where(norms == 0, 1, norms)).astype(np.float32)


def _handler(name: str) -> ToolHandler:
    async def handle(args: dict[str, Any]) -> ToolResult:
        return await _tool(name, args)

    return handle


def scripted_deps(recorded: list[dict[str, Any]]) -> AgentDeps:
    llm = ScriptedLLM(
        {"query_analyzer": _analyzer, "planner": _planner, "critic": _critic},
        {
            "synthesizer": _synthesizer,
            **{f"{a}_agent": _specialist for a in ("filings", "financials", "news", "screener")},
        },
        token_delay=TOKEN_DELAY_S,
    )
    tools = ScriptedTools(
        {
            n: _handler(n)
            for n in ("get_risk_factors", "search_news", "compare_risk_factors", "get_8k_events")
        },
        delay=TOOL_DELAY_S,
    )

    async def companies() -> list[Company]:
        return list(COMPANIES)

    async def record(run: dict[str, Any]) -> None:
        recorded.append(run)

    return AgentDeps(
        llm=llm,
        call_tool=tools,
        list_companies=companies,
        record_run=record,
        embed=bag_of_words_embed,
    )


@asynccontextmanager
async def scripted_runtime(
    settings: Settings, engine: AsyncEngine, wv: Any
) -> AsyncIterator[AgentRuntime]:
    """In-memory checkpointer and thread store: nothing is written to the database.
    Recorded runs stay in memory, so GET /api/runs/{id} has nothing for them."""
    recorded: list[dict[str, Any]] = []
    runtime = make_runtime(scripted_deps(recorded), memory_saver(), MemoryThreadStore(recorded))
    try:
        yield runtime
    finally:
        await runtime.runs.cancel_all()
