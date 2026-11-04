"""Graph state and reducers (BUILD_SPEC 4.2, DECISIONS D-002).

- `plan` is written only by the planner. A replan writes the old steps plus new ones,
  so nothing mutates a step in place and parallel branches never write it.
- `step_results` is keyed by step id with a merging reducer: parallel specialists write
  different keys, so their updates never conflict.
- `evidence` appends and dedupes by source_id. The RESET sentinel clears it (and
  `step_results`) at the start of each turn, so a follow-up question never cites the
  previous turn's sources, while `messages` keep the conversation.
"""

import operator
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from agent.tools.base import Evidence

AgentName = Literal["filings", "financials", "news", "screener"]
RESET = "__reset__"  # plain string: survives checkpoint serialization


class Entity(BaseModel):
    ticker: str
    cik: int
    name: str
    confidence: float
    matched_on: str  # the text in the question that matched


class Uncovered(BaseModel):
    """A company the question names that is not in the database."""

    mention: str
    close_matches: list[str] = Field(default_factory=list)


class Clarification(BaseModel):
    source: Literal["entity_resolver", "query_analyzer"]  # where the run resumes
    question: str
    options: list[str]
    answer: str | None = None


class Analysis(BaseModel):
    """query_analyzer's structured output (one LLM call)."""

    standalone_query: str = Field(
        description="The question rewritten to stand alone (resolve follow-ups like "
        "'do the same for Intel' using the conversation)"
    )
    advice_intent: bool = Field(
        description="True if the user asks whether to buy/sell/hold or invest in something"
    )
    needs_clarification: bool
    clarification_question: str
    clarification_options: list[str] = Field(description="2-4 concrete options, or empty")
    company_mentions: list[str] = Field(
        description="Every specific company the question refers to, as written"
    )


class ToolCallSpec(BaseModel):
    tool: str
    args_json: str = Field(description="JSON object with the tool's arguments")


class PlanStep(BaseModel):
    id: str
    goal: str
    agent: AgentName
    entities: list[str] = Field(default_factory=list)  # tickers
    depends_on: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallSpec] = Field(
        default_factory=list, description="Exact calls to run without an LLM (direct mode)"
    )


class ToolCallLog(BaseModel):
    tool: str
    args: dict[str, Any]
    ok: bool
    error: str | None = None
    evidence: int = 0
    latency_ms: int = 0


class StepResult(BaseModel):
    step_id: str
    status: Literal["done", "failed", "skipped"]
    mode: Literal["direct", "agentic", "none"]  # none = never ran (skipped)
    summary: str = ""
    data_digest: str = ""  # compact tool outputs for the critic/synthesizer/dependents
    evidence_ids: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallLog] = Field(default_factory=list)
    error: str | None = None
    skip_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Critique(BaseModel):
    verdict: Literal["ok", "gaps"]
    gaps: list[str] = Field(
        description="Missing information that more tool calls could still fetch"
    )
    unanswerable: list[str] = Field(
        description="Parts that cannot be answered with the available data (say so)"
    )
    notes: str


class Citation(BaseModel):
    n: int
    source_id: str
    title: str
    url: str | None


def merge_keyed(left: dict[str, Any] | None, right: dict[str, Any] | str) -> dict[str, Any]:
    if right == RESET:
        return {}
    return {**(left or {}), **right}  # type: ignore[dict-item]


def evidence_reducer(left: list[Evidence] | None, right: list[Evidence] | str) -> list[Evidence]:
    if isinstance(right, str):  # RESET
        return []
    seen = {e.source_id for e in left or []}
    out = list(left or [])
    for e in right:
        if e.source_id not in seen:
            seen.add(e.source_id)
            out.append(e)
    return out


class ResearchState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    turn: int
    today: str  # ISO date, injected into analyzer/planner/synthesizer prompts
    query: str
    route: Literal["research", "advice", "uncovered", "error"] | None
    run_id: str  # one id per question, kept across a clarification interrupt/resume
    resolved_entities: list[Entity]
    uncovered: list[Uncovered]
    clarification: Clarification | None
    clarification_count: int
    analysis: Analysis | None
    plan: list[PlanStep]
    plan_round: int
    step_results: Annotated[dict[str, StepResult], merge_keyed]
    evidence: Annotated[list[Evidence], evidence_reducer]
    dispatch_rounds: int
    critique: Critique | None
    replan_count: int
    answer: str | None
    evidence_order: list[str]  # source ids in [n] order, fixed by the synthesizer
    citations: list[Citation]


class SpecialistInput(TypedDict):
    """Payload of each Send() to a specialist subgraph."""

    step: PlanStep
    query: str
    today: str
    entities: list[str]
    dependency_context: list[str]


class SpecialistState(SpecialistInput, total=False):
    messages: list[AnyMessage]
    tool_log: Annotated[list[ToolCallLog], operator.add]
    evidence: Annotated[list[Evidence], evidence_reducer]
    digests: Annotated[list[str], operator.add]
    llm_turns: int
    mode: Literal["direct", "agentic"]
    direct_failure: str | None
    error: str | None
    started_at: datetime
    step_results: dict[str, StepResult]


class SpecialistOutput(TypedDict):
    step_results: dict[str, StepResult]
    evidence: list[Evidence]
