"""Shared tool contract: typed inputs/outputs, evidence for citations, no exceptions.

Every tool is an async function `(ctx, input_model) -> output_model`, registered with
`@tool`. The wrapper:
- validates input against the tool's Pydantic schema,
- enforces a timeout,
- converts *any* failure into `ToolResult(ok=False, error=...)` so a failing tool can
  never crash the agent graph (BUILD_SPEC 4.4),
- records latency.
Tools attach `Evidence` items (source id, URL, snippet) so Phase 3's synthesizer can cite
every claim with `[n]`.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import weaviate
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.embeddings import Embedder
from core.llm import GroqClient
from core.newsapi import NewsApiClient

logger = logging.getLogger(__name__)

SourceType = Literal["filing_chunk", "risk_factor", "8k", "financial_fact", "news", "sql"]


class Evidence(BaseModel):
    """One citable source. `source_id` is stable across runs (used to dedupe)."""

    source_id: str
    source_type: SourceType
    ticker: str | None = None
    title: str
    snippet: str = Field(description="The exact text supporting a claim (<= ~600 chars)")
    url: str | None = None
    form_type: str | None = None
    fiscal_year: int | None = None
    section: str | None = None
    filed_or_published: date | datetime | None = None
    score: float | None = None


class ToolError(BaseModel):
    code: Literal["invalid_input", "not_found", "timeout", "rejected", "upstream", "internal"]
    message: str


class ToolResult(BaseModel):
    tool: str
    ok: bool
    data: Any = None
    error: ToolError | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    latency_ms: int = 0


class ToolFailure(Exception):  # noqa: N818 - raised inside tools, surfaced as a ToolError
    """Raise inside a tool for an expected failure with a specific error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ToolContext:
    """Everything tools need. Built once per process (API, CLI, tests)."""

    settings: Settings
    engine: AsyncEngine
    weaviate: weaviate.WeaviateAsyncClient | None
    embedder: Embedder | None
    llm: GroqClient | None  # agent model (gpt-oss-120b during the full run, D-027)
    news: NewsApiClient | None = None  # set only when live NewsAPI fallback is allowed


@dataclass
class Output:
    """What a tool body returns: typed data + evidence + optional warnings."""

    data: BaseModel
    evidence: list[Evidence]
    warnings: list[str] | None = None


ToolFn = Callable[[ToolContext, Any], Awaitable[Output]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: ToolFn
    timeout_s: float


REGISTRY: dict[str, ToolSpec] = {}


def tool(
    name: str, input_model: type[BaseModel], timeout_s: float = 30.0
) -> Callable[[ToolFn], ToolFn]:
    """Register a tool. The docstring becomes the description the LLM sees."""

    def register(fn: ToolFn) -> ToolFn:
        REGISTRY[name] = ToolSpec(name, (fn.__doc__ or "").strip(), input_model, fn, timeout_s)
        return fn

    return register


async def call_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> ToolResult:
    """Run a registered tool and *always* return a ToolResult (never raise)."""
    started = time.monotonic()

    def done(**kw: Any) -> ToolResult:
        return ToolResult(tool=name, latency_ms=int((time.monotonic() - started) * 1000), **kw)

    spec = REGISTRY.get(name)
    if spec is None:
        return done(
            ok=False, error=ToolError(code="invalid_input", message=f"unknown tool {name!r}")
        )
    try:
        payload = spec.input_model.model_validate(args)
    except ValidationError as exc:
        return done(ok=False, error=ToolError(code="invalid_input", message=_short(exc)))
    try:
        out = await asyncio.wait_for(spec.fn(ctx, payload), timeout=spec.timeout_s)
    except TimeoutError:
        return done(
            ok=False, error=ToolError(code="timeout", message=f"exceeded {spec.timeout_s}s")
        )
    except ToolFailure as exc:
        return done(ok=False, error=ToolError(code=exc.code, message=str(exc)))
    except Exception as exc:  # last line of defence: report, don't crash the graph
        logger.exception("tool crashed", extra={"tool": name})
        return done(
            ok=False, error=ToolError(code="internal", message=f"{type(exc).__name__}: {exc}")
        )
    return done(
        ok=True,
        data=out.data.model_dump(mode="json"),
        evidence=out.evidence,
        warnings=out.warnings or [],
    )


def _short(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())[:500]


def snippet(text: str, limit: int = 600) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"
