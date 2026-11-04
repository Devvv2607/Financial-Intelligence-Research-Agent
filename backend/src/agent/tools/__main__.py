"""Call any agent tool from the command line against the real stores.

    python -m agent.tools list
    python -m agent.tools call get_risk_factors '{"ticker": "NVDA", "category": "supply_chain"}'
    python -m agent.tools demo          # one representative call per tool

Uses the agent model (AGENT_MODEL_*) for the one LLM-backed tool (query_database).
"""

import json
import sys
from typing import Any

from agent.tools import REGISTRY, build_context, call_tool, close_context
from core import aio
from core.config import get_settings
from core.logging import configure_logging

DEMO: list[tuple[str, dict[str, Any]]] = [
    (
        "search_filings",
        {
            "query": "export controls on chips sold to China",
            "tickers": ["NVDA", "AMD", "INTC"],
            "sections": ["item_1a"],
            "limit": 3,
        },
    ),
    ("get_risk_factors", {"ticker": "NVDA", "category": "supply_chain"}),
    ("compare_risk_factors", {"ticker": "AAPL"}),
    ("get_8k_events", {"ticker": "BA", "item_codes": ["2.02", "5.02", "8.01"], "limit": 5}),
    ("get_financial_metrics", {"tickers": ["NVDA", "AMD"], "metrics": ["revenue", "net_income"]}),
    (
        "calculate",
        {
            "expression": "pct_change(rev_new, rev_old)",
            "variables": {"rev_new": 130497000000, "rev_old": 60922000000},
        },
    ),
    ("search_news", {"query": "earnings guidance", "tickers": ["TSLA"], "limit": 3}),
    (
        "query_database",
        {
            "question": "Which companies flag supply_chain risks in their latest "
            "10-K, and what was their latest revenue growth?"
        },
    ),
]


def _print(name: str, result: Any) -> None:
    body = result.model_dump(mode="json")
    data = json.dumps(body["data"], indent=1, default=str)
    print(
        f"\n=== {name} | ok={body['ok']} | {body['latency_ms']} ms | "
        f"{len(body['evidence'])} evidence"
    )
    if body["error"]:
        print("error:", body["error"])
    if body["warnings"]:
        print("warnings:", body["warnings"])
    print(data[:2500] + ("\n…" if len(data) > 2500 else ""))


async def main(argv: list[str]) -> int:
    if not argv or argv[0] == "list":
        for spec in REGISTRY.values():
            print(f"{spec.name}: {spec.description.splitlines()[0]}")
        return 0
    ctx = await build_context(get_settings())
    try:
        if argv[0] == "call":
            name, args = argv[1], json.loads(argv[2] if len(argv) > 2 else "{}")
            _print(name, await call_tool(ctx, name, args))
        elif argv[0] == "demo":
            for name, args in DEMO:
                _print(name, await call_tool(ctx, name, args))
        else:
            print(__doc__)
            return 2
    finally:
        await close_context(ctx)
    return 0


if __name__ == "__main__":
    configure_logging("WARNING")
    sys.exit(aio.run(main(sys.argv[1:])))
