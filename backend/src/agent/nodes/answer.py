"""critic, synthesizer, finalize.

The critic decides between another planning round and answering (bounded: at most
MAX_REPLANS). The synthesizer writes the answer from *numbered* evidence only; finalize
checks every [n] against that numbering, drops any that don't exist, adds the fixed
footer, records the run, and appends the answer to the conversation memory.
"""

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from agent import prompts
from agent.deps import AgentDeps
from agent.nodes.intake import run_id_of
from agent.nodes.planning import _step_status_lines
from agent.state import Citation, Critique, ResearchState
from agent.tools.base import Evidence
from core.llm import DailyLimitError, LLMError

MAX_REPLANS = 2
MAX_SOURCES = 30
SOURCE_SNIPPET = 300
DIGEST_FOR_REVIEW = 1_200
FOOTER = (
    "\n\n---\n*Not investment advice. Sources: SEC EDGAR filings (10-K, 8-K, XBRL) and "
    "NewsAPI headlines, as cited above.*"
)
FOOTER_NO_SOURCES = "\n\n---\n*Not investment advice.*"
_CITE_RE = re.compile(r"\[(\d{1,3})\]")
# gpt-oss often cites in its native style: 【3】 or 【3†source】. Normalise to [3].
_NATIVE_CITE_RE = re.compile(r"\u3010\s*(\d{1,3})(?:[^\u3011]*)\u3011")
_NATIVE_OTHER_RE = re.compile(r"\u3010[^\u3011]*\u3011")


def _step_details(state: ResearchState) -> str:
    results = state.get("step_results", {})
    blocks = []
    for s in state.get("plan", []):
        r = results.get(s.id)
        if r is None:
            continue
        head = f"### {s.id} [{s.agent}, {r.status}, {r.mode}] {s.goal}"
        if r.status != "done":
            blocks.append(f"{head}\nNot available: {r.error or r.skip_reason}")
        else:
            blocks.append(f"{head}\n{r.summary}\n{r.data_digest[:DIGEST_FOR_REVIEW]}")
    return "\n\n".join(blocks)


def numbered_sources(state: ResearchState) -> list[Evidence]:
    """Evidence in step order (then any extra), deduped, capped: the [n] numbering."""
    by_id = {e.source_id: e for e in state.get("evidence", [])}
    order: list[str] = []
    for s in state.get("plan", []):
        r = state.get("step_results", {}).get(s.id)
        for sid in r.evidence_ids if r else []:
            if sid in by_id and sid not in order:
                order.append(sid)
    order += [sid for sid in by_id if sid not in order]
    return [by_id[sid] for sid in order[:MAX_SOURCES]]


def make_answer_nodes(deps: AgentDeps) -> dict[str, Any]:
    async def critic(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
        analysis = state.get("analysis")
        question = analysis.standalone_query if analysis else state["query"]
        user = "\n\n".join(
            [
                f"Today's date: {state['today']}",
                f"Question: {question}",
                "Uncovered companies: "
                + (", ".join(u.mention for u in state.get("uncovered", [])) or "none"),
                f"Steps:\n{_step_status_lines(state)}",
                f"Step outputs:\n{_step_details(state)}",
            ]
        )
        try:
            critique = await deps.llm.structured(
                "critic",
                Critique,
                [
                    {"role": "system", "content": prompts.load("critic")},
                    {"role": "user", "content": user},
                ],
                run_id=run_id_of(config),
            )
        except (DailyLimitError, LLMError) as exc:
            critique = Critique(
                verdict="ok",
                gaps=[],
                unanswerable=[],
                notes=f"Critic unavailable ({type(exc).__name__}); answering "
                "from the results collected.",
            )
        replan = (
            critique.verdict == "gaps"
            and bool(critique.gaps)
            and state.get("replan_count", 0) < MAX_REPLANS
        )
        get_stream_writer()(
            {
                "event": "critique",
                "verdict": critique.verdict,
                "gaps": critique.gaps,
                "replanning": replan,
            }
        )
        update: dict[str, Any] = {"critique": critique}
        if replan:
            update["replan_count"] = state.get("replan_count", 0) + 1
        return update

    async def synthesizer(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
        sources = numbered_sources(state)
        companies = {c.ticker: c for c in await deps.companies()}
        analysis = state.get("analysis")
        critique = state.get("critique")
        tickers = [e.ticker for e in state.get("resolved_entities", []) if e.ticker in companies]
        periods = await deps.fiscal_periods(tickers) if deps.fiscal_periods and tickers else {}
        fy = [
            f"{t}: " + "; ".join(f"FY{y} ended {d.isoformat()}" for y, d in periods[t])
            if periods.get(t)
            else f"{t}: fiscal year ends {companies[t].fiscal_year_end} (MMDD)"
            for t in tickers
        ]
        source_lines = [
            f"[{i}] {e.title}"
            + (
                f" ({e.form_type}" + (f", FY{e.fiscal_year}" if e.fiscal_year else "") + ")"
                if e.form_type
                else ""
            )
            + (f" {e.filed_or_published}" if e.filed_or_published else "")
            + f"\n{e.snippet[:SOURCE_SNIPPET]}"
            for i, e in enumerate(sources, start=1)
        ]
        limits = [f"- {u.mention}: not covered by the dataset" for u in state.get("uncovered", [])]
        limits += [f"- {g}" for g in (critique.unanswerable if critique else [])]
        user = "\n\n".join(
            [
                f"Today's date: {state['today']}",
                f"Question: {analysis.standalone_query if analysis else state['query']}",
                "Fiscal periods:\n" + ("\n".join(fy) or "n/a"),
                f"Step results:\n{_step_details(state)}",
                "Known limits:\n" + ("\n".join(limits) or "none"),
                "Sources:\n" + ("\n\n".join(source_lines) or "(no sources were retrieved)"),
            ]
        )
        try:
            reply = await deps.llm.chat(
                "synthesizer",
                [SystemMessage(content=prompts.load("synthesizer")), HumanMessage(content=user)],
                run_id=run_id_of(config),
                stream=True,
                config=config,
            )
            answer = str(reply.content).strip()
        except (DailyLimitError, LLMError) as exc:
            answer = (
                f"I gathered results but couldn't write the final answer ({type(exc).__name__}). "
                "Step summaries:\n"
                + "\n".join(
                    f"- {r.step_id}: {r.summary or r.error or r.skip_reason}"
                    for r in state.get("step_results", {}).values()
                )
            )
        return {"answer": answer, "evidence_order": [e.source_id for e in sources]}

    async def finalize(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
        answer = state.get("answer") or "I couldn't produce an answer."
        answer, citations = resolve_citations(answer, state)
        answer = answer.rstrip() + (FOOTER if citations else FOOTER_NO_SOURCES)
        cfg = config.get("configurable") or {}
        if deps.record_run is not None:
            await deps.record_run(
                {
                    "run_id": cfg.get("agent_run_id"),
                    "thread_id": cfg.get("thread_id"),
                    "query": state.get("query", ""),
                    "route": state.get("route"),
                    "plan": [s.model_dump(mode="json") for s in state.get("plan", [])],
                    "steps": {
                        k: v.model_dump(mode="json")
                        for k, v in state.get("step_results", {}).items()
                    },
                    "answer": answer,
                }
            )
        get_stream_writer()(
            {"event": "final", "answer": answer, "citations": [c.model_dump() for c in citations]}
        )
        return {"answer": answer, "citations": citations, "messages": [AIMessage(content=answer)]}

    return {"critic": critic, "synthesizer": synthesizer, "finalize": finalize}


def resolve_citations(answer: str, state: ResearchState) -> tuple[str, list[Citation]]:
    """Map [n] to sources; remove references to numbers that don't exist."""
    answer = _NATIVE_CITE_RE.sub(lambda m: f"[{m.group(1)}]", answer)
    answer = _NATIVE_OTHER_RE.sub("", answer)  # e.g. 【r1s1】: not a source number
    order = state.get("evidence_order", [])
    by_id = {e.source_id: e for e in state.get("evidence", [])}
    used: dict[int, Citation] = {}

    def check(m: re.Match[str]) -> str:
        n = int(m.group(1))
        if 1 <= n <= len(order) and order[n - 1] in by_id:
            e = by_id[order[n - 1]]
            used.setdefault(n, Citation(n=n, source_id=e.source_id, title=e.title, url=e.url))
            return m.group(0)
        return ""

    return _CITE_RE.sub(check, answer), [used[k] for k in sorted(used)]


def route_after_critic(state: ResearchState) -> str:
    critique = state.get("critique")
    replanning = (
        critique is not None
        and critique.verdict == "gaps"
        and bool(critique.gaps)
        and state.get("replan_count", 0) <= MAX_REPLANS
        and state.get("replan_count", 0) > _replans_done(state)
    )
    return "planner" if replanning else "synthesizer"


def _replans_done(state: ResearchState) -> int:
    return max(state.get("plan_round", 1) - 1, 0)
