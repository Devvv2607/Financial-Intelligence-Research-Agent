"""planner, dispatcher (Send fan-out), join.

The planner writes a DAG of steps. Direct tool calls it specifies are validated against
each tool's input schema here; an invalid one is dropped so the step runs agentically.
Step ids are made unique per planning round (r1s1, r2s1, ...) so replans can never collide
with earlier steps, and replans append to the plan without touching completed results.

The dispatcher sends every step whose dependencies are done to its specialist subgraph,
all in the same superstep (parallel). A step whose dependency failed or was skipped is
itself marked skipped with the reason, so nothing can wait forever (owner rule).
"""

import json
from datetime import UTC, datetime
from functools import cache
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.types import Send
from pydantic import BaseModel, Field, ValidationError

from agent import prompts
from agent.deps import AgentDeps
from agent.nodes.intake import run_id_of
from agent.state import PlanStep, ResearchState, SpecialistInput, StepResult, ToolCallSpec
from agent.tools import AGENT_TOOLS, REGISTRY
from core.llm import DailyLimitError, LLMError

MAX_NEW_STEPS = 6
MAX_DISPATCH_ROUNDS = 6
DIGEST_FOR_DEPENDENTS = 1_500


class PlannedStep(BaseModel):
    id: str
    goal: str
    agent: str
    entities: list[str]
    depends_on: list[str]
    tool_calls: list[ToolCallSpec]


class PlannerOutput(BaseModel):
    steps: list[PlannedStep] = Field(
        description="1-6 steps; [] only when replanning finds nothing to add"
    )


@cache
def tool_catalog() -> str:
    """Specialists, their tools and argument signatures, for the planner prompt (static)."""
    lines = []
    for agent, tools in AGENT_TOOLS.items():
        lines.append(f"### {agent}")
        for name in tools:
            spec = REGISTRY[name]
            schema = spec.input_model.model_json_schema()
            required = set(schema.get("required", []))
            args = ", ".join(
                f"{k}{'' if k in required else '?'}: {_type_of(v)}"
                for k, v in schema.get("properties", {}).items()
            )
            lines.append(f"- {name}({args}): {spec.description.split('.')[0].strip()}.")
    return "\n".join(lines)


def _type_of(prop: dict[str, Any]) -> str:
    if "enum" in prop:
        return "|".join(map(str, prop["enum"]))
    if "anyOf" in prop:
        return "|".join(_type_of(p) for p in prop["anyOf"] if p.get("type") != "null")
    if prop.get("type") == "array":
        return f"list[{_type_of(prop.get('items', {}))}]"
    return str(prop.get("type", "any"))


def validate_direct_call(agent: str, call: ToolCallSpec) -> str | None:
    """None if the planned call can run as-is, else why it can't (-> agentic)."""
    if call.tool not in AGENT_TOOLS.get(agent, ()):
        return f"{call.tool} is not a {agent} tool"
    try:
        args = json.loads(call.args_json or "{}")
        REGISTRY[call.tool].input_model.model_validate(args)
    except (ValueError, ValidationError) as exc:
        return f"invalid arguments for {call.tool}: {str(exc)[:200]}"
    return None


def normalize_plan(
    planned: list[PlannedStep], existing: list[PlanStep], round_no: int, tickers: set[str]
) -> list[PlanStep]:
    """Rename ids to r{round}s{k}, keep only valid dependencies (earlier steps or existing
    ones: no cycles possible), filter entities to resolved tickers, validate direct calls."""
    rename: dict[str, str] = {}
    known = {s.id for s in existing}
    out: list[PlanStep] = []
    for k, p in enumerate(planned[:MAX_NEW_STEPS], start=1):
        if p.agent not in AGENT_TOOLS:
            continue
        new_id = f"r{round_no}s{k}"
        deps = [rename.get(d, d) for d in p.depends_on]
        deps = [d for d in dict.fromkeys(deps) if d in known or d in rename.values()]
        calls = [c for c in p.tool_calls if validate_direct_call(p.agent, c) is None]
        out.append(
            PlanStep(
                id=new_id,
                goal=p.goal,
                agent=p.agent,
                entities=[t for t in (e.upper() for e in p.entities) if t in tickers],
                depends_on=deps,
                tool_calls=calls,
            )
        )
        rename[p.id] = new_id
    return out


def _step_status_lines(state: ResearchState) -> str:
    results = state.get("step_results", {})
    lines = []
    for s in state.get("plan", []):
        r = results.get(s.id)
        status = f"{r.status} ({r.mode})" if r else "pending"
        detail = (r.summary or r.error or r.skip_reason or "")[:400] if r else ""
        lines.append(f"- {s.id} [{s.agent}] {s.goal} -> {status}. {detail}")
    return "\n".join(lines)


def make_planning_nodes(deps: AgentDeps) -> dict[str, Any]:
    async def planner(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
        round_no = state.get("plan_round", 0) + 1
        analysis = state.get("analysis")
        question = analysis.standalone_query if analysis else state["query"]
        companies = {c.ticker: c for c in await deps.companies()}
        entities = state.get("resolved_entities", [])
        lines = [
            f"Today's date: {state['today']}",
            f"Question: {question}",
            "Resolved tickers: "
            + (
                ", ".join(
                    f"{e.ticker} ({e.name}, fiscal year ends "
                    f"{companies[e.ticker].fiscal_year_end or '?'} MMDD)"
                    for e in entities
                    if e.ticker in companies
                )
                or "none (sector-wide question)"
            ),
        ]
        if state.get("uncovered"):
            lines.append(
                "Not covered (do not plan for these): "
                + ", ".join(u.mention for u in state["uncovered"])
            )
        existing = state.get("plan", [])
        critique = state.get("critique")
        if existing:
            lines.append("Steps already run:\n" + _step_status_lines(state))
            if critique:
                lines.append("Gaps to fill:\n" + "\n".join(f"- {g}" for g in critique.gaps))
        try:
            out = await deps.llm.structured(
                "planner",
                PlannerOutput,
                [
                    {
                        "role": "system",
                        "content": prompts.load("planner").replace(
                            "{tool_catalog}", tool_catalog()
                        ),
                    },
                    {"role": "user", "content": "\n\n".join(lines)},
                ],
                run_id=run_id_of(config),
            )
        except (DailyLimitError, LLMError) as exc:
            if existing:  # a failed replan still lets us answer from what we have
                return {"plan_round": round_no}
            return {"route": "error", "answer": f"I couldn't plan the research: {exc}"}
        new_steps = normalize_plan(out.steps, existing, round_no, set(companies))
        if not new_steps and not existing:
            return {
                "route": "error",
                "answer": "I couldn't turn this question into research steps.",
            }
        get_stream_writer()(
            {
                "event": "replan" if existing else "plan_created",
                "round": round_no,
                "steps": [s.model_dump() for s in new_steps],
            }
        )
        return {"plan": existing + new_steps, "plan_round": round_no}

    async def dispatcher(state: ResearchState) -> dict[str, Any]:
        rounds = state.get("dispatch_rounds", 0) + 1
        ready, skipped = ready_and_skipped(state, over_limit=rounds > MAX_DISPATCH_ROUNDS)
        writer = get_stream_writer()
        for step_id, res in skipped.items():
            writer(
                {
                    "event": "step_completed",
                    "step_id": step_id,
                    "status": "skipped",
                    "reason": res.skip_reason,
                }
            )
        for s in ready:
            writer(
                {
                    "event": "step_started",
                    "step_id": s.id,
                    "agent": s.agent,
                    "mode": "direct" if s.tool_calls else "agentic",
                }
            )
        return {"dispatch_rounds": rounds, "step_results": skipped}

    async def join(state: ResearchState) -> dict[str, Any]:
        return {}

    return {"planner": planner, "dispatcher": dispatcher, "join": join}


def ready_and_skipped(
    state: ResearchState, *, over_limit: bool = False
) -> tuple[list[PlanStep], dict[str, StepResult]]:
    results = dict(state.get("step_results", {}))
    pending = [s for s in state.get("plan", []) if s.id not in results]
    skipped: dict[str, StepResult] = {}
    now = datetime.now(UTC)
    changed = True
    while changed:  # propagate through chains of dependents
        changed = False
        for s in pending:
            if s.id in skipped:
                continue
            for d in s.depends_on:
                dep = results.get(d) or skipped.get(d)
                if dep is not None and dep.status in ("failed", "skipped"):
                    why = dep.error or dep.skip_reason or "no result"
                    skipped[s.id] = StepResult(
                        step_id=s.id,
                        status="skipped",
                        mode="none",
                        skip_reason=f"depends on {d}, which {dep.status}: {why}"[:300],
                        finished_at=now,
                    )
                    changed = True
                    break
    remaining = [s for s in pending if s.id not in skipped]
    ready = [
        s
        for s in remaining
        if all(results.get(d) is not None and results[d].status == "done" for d in s.depends_on)
    ]
    if over_limit or (not ready and remaining):
        reason = "dispatch round limit reached" if over_limit else "unresolvable dependency"
        for s in remaining:
            skipped[s.id] = StepResult(
                step_id=s.id, status="skipped", mode="none", skip_reason=reason, finished_at=now
            )
        ready = []
    return ready, skipped


def dependency_context(state: ResearchState, step: PlanStep) -> list[str]:
    results = state.get("step_results", {})
    out = []
    for d in step.depends_on:
        r = results.get(d)
        if r is not None and r.status == "done":
            out.append(f"Result of {d}: {r.summary}\n{r.data_digest[:DIGEST_FOR_DEPENDENTS]}")
    return out


def route_after_dispatch(state: ResearchState) -> list[Send] | str:
    ready, _ = ready_and_skipped(state)
    if not ready:
        return "critic"
    entities = [e.ticker for e in state.get("resolved_entities", [])]
    analysis = state.get("analysis")
    return [
        Send(
            f"{s.agent}_agent",
            SpecialistInput(
                step=s,
                query=analysis.standalone_query if analysis else state["query"],
                today=state["today"],
                entities=s.entities or entities,
                dependency_context=dependency_context(state, s),
            ),
        )
        for s in ready
    ]
