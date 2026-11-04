"""Scripted stand-ins for the LLM, the tools and the company list (no network)."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from agent.deps import AgentDeps
from agent.tools.base import Evidence, ToolError, ToolResult
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

Script = list[Any] | Callable[[Sequence[Any]], Any]


class FakeLLM:
    """Answers per node from a queue (or a function of the messages) and records calls."""

    def __init__(
        self, structured: dict[str, Script] | None = None, chat: dict[str, Script] | None = None
    ) -> None:
        self._structured = structured or {}
        self._chat = chat or {}
        self.calls: list[tuple[str, Any]] = []

    @staticmethod
    def _next(script: Script, messages: Sequence[Any]) -> Any:
        if callable(script):
            return script(messages)
        if not script:
            raise AssertionError("script exhausted")
        return script.pop(0) if len(script) > 1 else script[0]

    async def structured(
        self,
        node: str,
        schema: type[BaseModel],
        messages: list[dict[str, str]],
        *,
        run_id: str | None,
    ) -> Any:
        self.calls.append((node, messages))
        if node not in self._structured:
            raise AssertionError(f"unexpected structured call for {node}")
        value = self._next(self._structured[node], messages)
        return value if isinstance(value, BaseModel) else schema.model_validate(value)

    async def chat(
        self,
        node: str,
        messages: Sequence[BaseMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        stream: bool = False,
        config: Any = None,
    ) -> AIMessage:
        self.calls.append((node, list(messages)))
        if node not in self._chat:
            raise AssertionError(f"unexpected chat call for {node}")
        value = self._next(self._chat[node], messages)
        return value if isinstance(value, AIMessage) else AIMessage(content=str(value))

    def count(self, node: str) -> int:
        return sum(1 for n, _ in self.calls if n == node)


def ev(source_id: str, ticker: str = "NVDA") -> Evidence:
    return Evidence(
        source_id=source_id,
        source_type="risk_factor",
        ticker=ticker,
        title=f"{ticker} source {source_id}",
        snippet="supporting text",
        url=f"https://www.sec.gov/{source_id}",
    )


class FakeTools:
    """Tool executor with per-tool handlers; records (name, args, start, end)."""

    def __init__(
        self,
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[ToolResult]]],
        delay: float = 0.0,
    ) -> None:
        self.handlers = handlers
        self.delay = delay
        self.log: list[tuple[str, dict[str, Any], float, float]] = []

    async def __call__(self, name: str, args: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        await asyncio.sleep(self.delay)
        handler = self.handlers.get(name)
        result = (
            await handler(args)
            if handler
            else ToolResult(
                tool=name, ok=False, error=ToolError(code="not_found", message="no handler")
            )
        )
        self.log.append((name, args, start, time.monotonic()))
        return result


def ok(tool: str, data: dict[str, Any], evidence: list[Evidence]) -> Callable[..., Any]:
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, ok=True, data=data, evidence=evidence)

    return handler


def failing(tool: str) -> Callable[..., Any]:
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, ok=False, error=ToolError(code="upstream", message="boom"))

    return handler


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
