"""Specialist subgraphs (filings, financials, news, screener): one compiled graph each.

    prepare ─► direct ─(ok)─────────────────────────► finish
       │          └─(error / empty) ─► llm ◄─┐
       └──────────────────────────────► llm ─┼─(tool calls, < MAX)─► tools
                                             └─(answer or limit)───► finish

Direct mode runs the planner's exact tool calls with no LLM at all. If a direct call
errors or returns nothing, the step falls back to the model-driven loop (owner rule) and
StepResult.mode records which path produced the result.
"""

import json
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent import prompts
from agent.deps import AgentDeps
from agent.nodes.intake import run_id_of
from agent.state import (
    AgentName,
    SpecialistInput,
    SpecialistOutput,
    SpecialistState,
    StepResult,
    ToolCallLog,
)
from agent.tools import AGENT_TOOLS, REGISTRY, ToolResult
from core.llm import DailyLimitError, LLMError

MAX_TOOL_CALLS = 4
TOOL_OUTPUT_FOR_LLM = 2_500  # chars of tool data the model sees per call
DIGEST_PER_CALL = 1_200
DIGEST_TOTAL = 3_500


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline_refs(defs[node["$ref"].split("/")[-1]], defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def tool_schemas(agent: AgentName) -> list[dict[str, Any]]:
    """OpenAI-style function schemas for the agent's tools ($defs inlined)."""
    out = []
    for name in AGENT_TOOLS[agent]:
        spec = REGISTRY[name]
        schema = spec.input_model.model_json_schema()
        defs = schema.pop("$defs", {})
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.description,
                    "parameters": _inline_refs(schema, defs),
                },
            }
        )
    return out


def _compact(result: ToolResult, limit: int) -> str:
    if not result.ok:
        return f"ERROR ({result.error.code if result.error else '?'}): " + (
            result.error.message if result.error else "unknown"
        )
    text = json.dumps(result.data, default=str, separators=(",", ":"))
    note = f" [warnings: {'; '.join(result.warnings)}]" if result.warnings else ""
    return (text[:limit] + ("…" if len(text) > limit else "")) + note


def _has_content(result: ToolResult) -> bool:
    if not result.ok:
        return False
    if result.evidence:
        return True
    data = result.data or {}
    return any(bool(v) for v in data.values()) if isinstance(data, dict) else bool(data)


def build_specialist(agent: AgentName, deps: AgentDeps) -> CompiledStateGraph:  # type: ignore[type-arg]
    node_name = f"{agent}_agent"
    schemas = tool_schemas(agent)

    async def run_tool(
        name: str, args: dict[str, Any], step_id: str
    ) -> tuple[ToolResult, ToolCallLog]:
        result = await deps.call_tool(name, args)
        log = ToolCallLog(
            tool=name,
            args=args,
            ok=result.ok,
            error=result.error.message if result.error else None,
            evidence=len(result.evidence),
            latency_ms=result.latency_ms,
        )
        writer = get_stream_writer()
        writer(
            {
                "event": "tool_called",
                "step_id": step_id,
                "tool": name,
                "args": args,
                "ok": result.ok,
                "evidence": len(result.evidence),
            }
        )
        if result.evidence:
            writer(
                {
                    "event": "evidence_added",
                    "step_id": step_id,
                    # full cards, not just ids: the UI's Sources tab renders them live
                    "sources": [e.model_dump(mode="json") for e in result.evidence],
                }
            )
        return result, log

    async def prepare(state: SpecialistState) -> dict[str, Any]:
        step = state["step"]
        context = "\n\n".join(state.get("dependency_context", []))
        user = (
            f"Step {step.id}: {step.goal}\n"
            f"Tickers: {', '.join(state.get('entities', [])) or 'none given'}\n"
            f"Overall question (for context): {state['query']}"
            + (f"\n\nResults of earlier steps:\n{context}" if context else "")
        )
        system = (
            prompts.load("specialist")
            .replace("{agent}", agent)
            .replace("{max_calls}", str(MAX_TOOL_CALLS))
            .replace("{today}", state["today"])
        )
        return {
            "messages": [SystemMessage(content=system), HumanMessage(content=user)],
            "llm_turns": 0,
            "started_at": datetime.now(UTC),
            "mode": "direct" if step.tool_calls else "agentic",
        }

    async def direct(state: SpecialistState) -> dict[str, Any]:
        step = state["step"]
        logs, evidence, digests, failure = [], [], [], None
        for call in step.tool_calls:
            args = json.loads(call.args_json or "{}")
            result, log = await run_tool(call.tool, args, step.id)
            logs.append(log)
            evidence.extend(result.evidence)
            if _has_content(result):
                digests.append(f"{call.tool}: {_compact(result, DIGEST_PER_CALL)}")
            elif failure is None:
                failure = f"{call.tool}({call.args_json}) " + (
                    f"failed: {log.error}" if not result.ok else "returned nothing"
                )
        update: dict[str, Any] = {"tool_log": logs, "evidence": evidence, "digests": digests}
        if failure:
            update["mode"] = "agentic"
            update["direct_failure"] = failure
            update["messages"] = [
                *state["messages"],
                HumanMessage(
                    content=(f"A planned direct call {failure}. Complete the step with your tools.")
                ),
            ]
        return update

    async def llm(state: SpecialistState, config: RunnableConfig) -> dict[str, Any]:
        calls_made = len(state.get("tool_log", []))
        try:
            reply = await deps.llm.chat(
                node_name,
                state["messages"],
                tools=schemas if calls_made < MAX_TOOL_CALLS else None,
                run_id=run_id_of(config),
            )
        except (DailyLimitError, LLMError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"messages": [*state["messages"], reply], "llm_turns": state.get("llm_turns", 0) + 1}

    async def tools(state: SpecialistState) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        budget = MAX_TOOL_CALLS - len(state.get("tool_log", []))
        messages = list(state["messages"])
        logs, evidence, digests = [], [], []
        for i, tc in enumerate(last.tool_calls):
            if i >= budget:
                messages.append(
                    ToolMessage(
                        content="Tool-call limit reached; answer now.", tool_call_id=tc["id"] or ""
                    )
                )
                continue
            result, log = await run_tool(tc["name"], tc["args"], state["step"].id)
            logs.append(log)
            evidence.extend(result.evidence)
            if _has_content(result):
                digests.append(f"{tc['name']}: {_compact(result, DIGEST_PER_CALL)}")
            messages.append(
                ToolMessage(
                    content=_compact(result, TOOL_OUTPUT_FOR_LLM), tool_call_id=tc["id"] or ""
                )
            )
        return {"messages": messages, "tool_log": logs, "evidence": evidence, "digests": digests}

    async def finish(state: SpecialistState) -> dict[str, Any]:
        step = state["step"]
        log = state.get("tool_log", [])
        mode = state.get("mode", "agentic")
        error = state.get("error")
        if mode == "direct":
            ok = not error
            summary = (
                "; ".join(f"{entry.tool}: {entry.evidence} sources" for entry in log)
                + " (direct calls)"
            )
        else:
            last_ai = next(
                (m for m in reversed(state.get("messages", [])) if isinstance(m, AIMessage)), None
            )
            summary = str(last_ai.content).strip() if last_ai and last_ai.content else ""
            ok = not error and any(entry.ok for entry in log)
            if state.get("direct_failure"):
                summary = f"(direct call fell back to agentic: {state['direct_failure']}) {summary}"
        evidence = state.get("evidence", [])
        digest = "\n".join(state.get("digests", []))[:DIGEST_TOTAL]
        result = StepResult(
            step_id=step.id,
            status="done" if ok else "failed",
            mode=mode,
            summary=summary[:1_500],
            data_digest=digest,
            evidence_ids=[e.source_id for e in evidence],
            tool_calls=log,
            error=error or (None if ok else "no successful tool result"),
            started_at=state.get("started_at"),
            finished_at=datetime.now(UTC),
        )
        get_stream_writer()(
            {
                "event": "step_completed",
                "step_id": step.id,
                "status": result.status,
                "mode": mode,
                "summary": result.summary[:300],
                "error": result.error,
                "duration_ms": _duration_ms(result),
            }
        )
        return {"step_results": {step.id: result}, "evidence": evidence}

    def after_prepare(state: SpecialistState) -> str:
        return "direct" if state["step"].tool_calls else "llm"

    def after_direct(state: SpecialistState) -> str:
        return "llm" if state.get("mode") == "agentic" else "finish"

    def after_llm(state: SpecialistState) -> str:
        if state.get("error"):
            return "finish"
        last = state["messages"][-1]
        wants_tools = isinstance(last, AIMessage) and bool(last.tool_calls)
        return (
            "tools" if wants_tools and len(state.get("tool_log", [])) < MAX_TOOL_CALLS else "finish"
        )

    g = StateGraph(SpecialistState, input_schema=SpecialistInput, output_schema=SpecialistOutput)
    g.add_node("prepare", prepare)
    g.add_node("direct", direct)
    g.add_node("llm", llm)
    g.add_node("tools", tools)
    g.add_node("finish", finish)
    g.add_edge(START, "prepare")
    g.add_conditional_edges("prepare", after_prepare, ["direct", "llm"])
    g.add_conditional_edges("direct", after_direct, ["llm", "finish"])
    g.add_conditional_edges("llm", after_llm, ["tools", "finish"])
    g.add_edge("tools", "llm")
    g.add_edge("finish", END)
    return g.compile(name=node_name)


def _duration_ms(r: StepResult) -> int | None:
    if r.started_at is None or r.finished_at is None:
        return None
    return int((r.finished_at - r.started_at).total_seconds() * 1000)
