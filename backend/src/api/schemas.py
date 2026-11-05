"""Request/response models and the typed agent events streamed over SSE.

Every event the graph emits (via `get_stream_writer`) is validated against
`AgentEvent` before it is sent, so the wire format is a contract the frontend can rely
on: one JSON object per SSE message, `event:` = its `event` field.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter

from agent.state import Citation, PlanStep, StepResult
from agent.tools.base import Evidence
from core.threads import Thread, ThreadStatus

__all__ = ["Thread", "ThreadStatus"]  # re-exported: part of the API's schema


# ---------------------------------------------------------------- requests
class CreateThread(BaseModel):
    title: str | None = Field(None, max_length=200)


class RunRequest(BaseModel):
    query: str = Field(min_length=1)


class ResumeRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------- events
class _Event(BaseModel):
    pass


class RunStarted(_Event):
    event: Literal["run_started"]
    run_id: str
    thread_id: str
    resumed: bool = False


class PlanCreated(_Event):
    event: Literal["plan_created"]
    round: int
    steps: list[PlanStep]


class Replan(_Event):
    event: Literal["replan"]
    round: int
    steps: list[PlanStep]


class StepStarted(_Event):
    event: Literal["step_started"]
    step_id: str
    agent: str
    mode: Literal["direct", "agentic"]


class ToolCalled(_Event):
    event: Literal["tool_called"]
    step_id: str
    tool: str
    args: dict[str, Any]
    ok: bool
    evidence: int


class EvidenceAdded(_Event):
    event: Literal["evidence_added"]
    step_id: str
    sources: list[Evidence]


class StepCompleted(_Event):
    event: Literal["step_completed"]
    step_id: str
    status: Literal["done", "failed", "skipped"]
    mode: Literal["direct", "agentic", "none"] | None = None
    summary: str | None = None
    error: str | None = None
    reason: str | None = None  # why a step was skipped
    duration_ms: int | None = None


class CritiqueEvent(_Event):
    event: Literal["critique"]
    verdict: Literal["ok", "gaps"]
    gaps: list[str]
    replanning: bool


class ClarificationNeeded(_Event):
    event: Literal["clarification_needed"]
    question: str
    options: list[str]
    source: Literal["entity_resolver", "query_analyzer"] | None = None


class Token(_Event):
    event: Literal["token"]
    text: str


class Final(_Event):
    event: Literal["final"]
    answer: str
    citations: list[Citation]
    citation_check: dict[str, Any] | None = None


class ErrorEvent(_Event):
    event: Literal["error"]
    code: Literal["agent_error", "internal"]
    message: str


AgentEvent = Annotated[
    RunStarted
    | PlanCreated
    | Replan
    | StepStarted
    | ToolCalled
    | EvidenceAdded
    | StepCompleted
    | CritiqueEvent
    | ClarificationNeeded
    | Token
    | Final
    | ErrorEvent,
    Field(discriminator="event"),
]
AGENT_EVENT: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)


# ---------------------------------------------------------------- threads
class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class PendingClarification(BaseModel):
    question: str
    options: list[str]
    source: str | None = None


class LatestTurn(BaseModel):
    """The most recent turn's detail, as the checkpointer holds it."""

    run_id: str | None
    query: str | None
    route: str | None
    plan: list[PlanStep]
    step_results: dict[str, StepResult]
    evidence: list[Evidence]
    citations: list[Citation]
    citation_check: dict[str, Any] | None = None


class ThreadDetail(BaseModel):
    thread: Thread
    messages: list[Message]
    pending_clarification: PendingClarification | None
    latest: LatestTurn | None
    run_ids: list[UUID]  # every recorded run on this thread, oldest first


# ---------------------------------------------------------------- graph
class GraphNode(BaseModel):
    id: str
    label: str
    subgraph: str | None = None  # the specialist a node belongs to, if any


class GraphEdge(BaseModel):
    source: str
    target: str
    conditional: bool


class GraphJSON(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# ---------------------------------------------------------------- companies
class CompanySummary(BaseModel):
    ticker: str
    name: str
    sector: str | None
    sic_sector: str | None
    llm_sector: str | None
    sector_conflict: bool | None
    fiscal_year_end: str | None
    latest_10k_year: int | None
    risk_factor_count: int


class RiskFactorItem(BaseModel):
    id: str
    heading: str
    summary: str | None
    category: str | None
    needs_review: bool


class RiskCategory(BaseModel):
    category: str
    count: int
    risk_factors: list[RiskFactorItem]


class EightKItem(BaseModel):
    accession_no: str
    filing_date: date
    items: list[str]
    url: str


class MetricPoint(BaseModel):
    fiscal_year: int
    value: float
    unit: str
    period_end: date


class CompanyDetail(BaseModel):
    ticker: str
    name: str
    cik: int
    sector: str | None
    sic: int | None
    sic_description: str | None
    sic_sector: str | None
    llm_sector: str | None
    llm_sub_industry: str | None
    llm_sector_rationale: str | None
    sector_conflict: bool | None
    fiscal_year_end: str | None
    risk_fiscal_year: int | None
    risk_breakdown: list[RiskCategory]
    recent_8ks: list[EightKItem]
    metrics: dict[str, list[MetricPoint]]


# ---------------------------------------------------------------- insights / runs
class HeatmapCell(BaseModel):
    sector: str
    category: str
    count: int
    companies: int


class IngestionStats(BaseModel):
    companies: int
    filings: int
    risk_factors: int
    eight_k_events: int
    financial_facts: int
    news_articles: int
    chunks: int | None  # Weaviate FilingChunk objects; None if Weaviate is unreachable


class RiskHeatmap(BaseModel):
    sectors: list[str]
    categories: list[str]
    cells: list[HeatmapCell]
    stats: IngestionStats


class RunDetail(BaseModel):
    id: UUID
    thread_id: str
    query: str
    route: str | None
    answer: str | None
    plan: list[dict[str, Any]] | None
    steps: dict[str, Any] | None
    node_usage: dict[str, Any] | None
    citation_check: dict[str, Any] | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    cost_usd: float | None
    created_at: datetime
