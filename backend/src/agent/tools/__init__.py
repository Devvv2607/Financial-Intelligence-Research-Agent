"""Agent tools (BUILD_SPEC 4.4). Importing this package registers every tool."""

from agent.tools import filings, financials, news, screener  # noqa: F401 - registration
from agent.tools.base import REGISTRY, ToolContext, ToolResult, call_tool
from agent.tools.context import build_context, close_context

# Which specialist agent owns which tools (used by the Phase 3 subgraphs).
AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    "filings": ("search_filings", "get_risk_factors", "compare_risk_factors", "get_8k_events"),
    "financials": ("get_financial_metrics", "calculate"),
    "news": ("search_news",),
    "screener": ("query_database",),
}

__all__ = [
    "AGENT_TOOLS", "REGISTRY", "ToolContext", "ToolResult", "build_context", "call_tool",
    "close_context",
]  # fmt: skip
