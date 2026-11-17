"""critic, synthesizer, finalize.

The critic decides between another planning round and answering (bounded: at most
MAX_REPLANS). The synthesizer writes the answer from *numbered* evidence only; finalize
checks every [n] against that numbering, drops any that don't exist, adds the fixed
footer, records the run, and appends the answer to the conversation memory.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from agent import prompts
from agent.citecheck import CitationReport, verify_citations
from agent.deps import AgentDeps
from agent.nodes.intake import run_id_of
from agent.nodes.planning import _step_status_lines
from agent.state import Citation, Critique, ResearchState
from agent.tools.base import Evidence
from agent.tools.common import FRESHNESS_NOTE_RE
from core.llm import DailyLimitError, LLMError

MAX_REPLANS = 2
MAX_SOURCES = 30
MAX_PER_STEP = 12
SOURCE_SNIPPET = 200
DIGEST_FOR_REVIEW = 1_200
FOOTER = (
    "\n\n---\n*Not investment advice. Sources: SEC EDGAR filings (10-K, 8-K, XBRL) and "
    "NewsAPI headlines, as cited above.*"
)
UNVERIFIED_NOTE = "\n*Claims marked [unverified] could not be matched to any retrieved source.*"
FOOTER_NO_SOURCES = "\n\n---\n*Not investment advice.*"
STALE_CHECK_DAYS = 2
logger = logging.getLogger(__name__)
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
            digest = r.data_digest[:DIGEST_FOR_REVIEW]
            if len(r.data_digest) > DIGEST_FOR_REVIEW:
                # Without this the critic took the cut for missing data and replanned to
                # re-fetch what the step already had (demo 7: two wasted rounds).
                digest += "\n[shortened for this view; the step retrieved the full output]"
            blocks.append(f"{head}\n{r.summary}\n{digest}")
    return "\n\n".join(blocks)


def numbered_sources(state: ResearchState) -> list[Evidence]:
    """The [n] numbering: each step's top MAX_PER_STEP evidence, round-robin, deduped, capped.

    Round-robin so one large step (53 AMD risk factors) can't use up the cap and leave
    other companies or the news without citable sources. "Top" is the tool's own order:
    tools return the most relevant items first, and on the 2026-11-04 runs that kept far
    more of the cited sources than ranking by embedding similarity to the question."""
    by_id = {e.source_id: e for e in state.get("evidence", [])}
    queues = []
    in_steps: set[str] = set()
    for s in state.get("plan", []):
        r = state.get("step_results", {}).get(s.id)
        ids = [sid for sid in (r.evidence_ids if r else []) if sid in by_id]
        in_steps.update(ids)
        queues.append(ids[:MAX_PER_STEP])
    order: list[str] = []
    for rank in range(max((len(q) for q in queues), default=0)):
        for q in queues:
            if rank < len(q) and q[rank] not in order:
                order.append(q[rank])
    order += [sid for sid in by_id if sid not in in_steps]  # evidence of no step (rare)
    chosen = [by_id[sid] for sid in order[:MAX_SOURCES]]
    # Primary sources (SEC filings) take the first numbers, news the rest: the synthesizer
    # tends to cite in number order, and the filing is what should lead (stable sort).
    return sorted(chosen, key=lambda e: e.source_type == "news")


def dated_label(e: Evidence) -> str:
    """'Form 8-K filed 12 Nov 2026' / 'published 15 Nov 2026', so the answer can quote an
    exact date instead of 'mid-November'."""
    when = e.filed_or_published
    if when is None:
        return ""
    day = f"{when.day} {when:%b %Y}"
    if e.source_type == "news":
        return f"published {day}"
    return f"Form {e.form_type} filed {day}" if e.form_type else f"dated {day}"


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
        critique = drop_detail_gaps(critique)
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
        tickers = [e.ticker for e in state.get("resolved_entities", []) if e.ticker in companies]
        periods = await deps.fiscal_periods(tickers) if deps.fiscal_periods and tickers else {}
        fy = [
            f"{t}: " + "; ".join(f"FY{y} ended {d.isoformat()}" for y, d in periods[t])
            if periods.get(t)
            else f"{t}: fiscal year ends {companies[t].fiscal_year_end} (MMDD)"
            for t in tickers
        ]
        user = "\n\n".join(s for _, s in synthesis_sections(state, sources, fy))
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
        answer = normalize_citation_style(state.get("answer") or "I couldn't produce an answer.")
        report: CitationReport | None = None
        order = state.get("evidence_order", [])
        by_id = {e.source_id: e for e in state.get("evidence", [])}
        if deps.embed is not None and order and all(sid in by_id for sid in order):
            try:
                answer, report = await verify_citations(
                    answer, [by_id[sid] for sid in order], deps.embed
                )
            except Exception:  # a local-model failure must not cost the user the answer
                logger.exception("citation check failed", extra={"run_id": run_id_of(config)})
        answer, citations = resolve_citations(answer, state)
        answer, citations, renumbered = renumber_citations(answer, citations)
        footer = FOOTER if citations else FOOTER_NO_SOURCES
        freshness = freshness_footer(state)
        if freshness:
            footer += "\n" + freshness
        if report and report.unverified_claims:
            footer += UNVERIFIED_NOTE
        answer = answer.rstrip() + footer
        check = report.summary() if report else None
        if check:
            logger.info("citation check", extra={"run_id": run_id_of(config), **check})
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
                    "citation_check": check,
                }
            )
        get_stream_writer()(
            {
                "event": "final",
                "answer": answer,
                "citations": [c.model_dump() for c in citations],
                "citation_check": check,
                "renumbered": {str(old): new for old, new in renumbered.items()},
            }
        )
        return {
            "answer": answer,
            "citations": citations,
            "citation_check": check,
            "messages": [AIMessage(content=answer)],
        }

    return {"critic": critic, "synthesizer": synthesizer, "finalize": finalize}


def source_text(e: Evidence, limit: int = SOURCE_SNIPPET) -> str:
    """The snippet as the synthesizer sees it: without the heading already in the title
    (risk factors and news repeat it), cut at a word boundary."""
    snippet = e.snippet
    heading = e.title.split(": ", 1)[-1]
    if len(heading) >= 20 and snippet.lower().startswith(heading.lower()):
        snippet = snippet[len(heading) :].lstrip(" .:-")
    if len(snippet) > limit:
        snippet = snippet[:limit].rsplit(" ", 1)[0] + "…"
    return snippet


def synthesis_sections(
    state: ResearchState, sources: list[Evidence], fiscal_periods: list[str]
) -> list[tuple[str, str]]:
    """The synthesizer's user message as named sections (named so their size can be measured)."""
    analysis = state.get("analysis")
    critique = state.get("critique")
    source_lines = [
        f"[{i}] {e.title}"
        + (
            f" ({e.form_type}" + (f", FY{e.fiscal_year}" if e.fiscal_year else "") + ")"
            if e.form_type
            else ""
        )
        + (f" — {dated_label(e)}" if e.filed_or_published else "")
        + f"\n{source_text(e)}"
        for i, e in enumerate(sources, start=1)
    ]
    limits = [f"- {u.mention}: not covered by the dataset" for u in state.get("uncovered", [])]
    limits += [f"- {g}" for g in (critique.unanswerable if critique else [])]
    freshness = freshness_lines(state)
    return [
        ("header", f"Today's date: {state['today']}"),
        ("question", f"Question: {analysis.standalone_query if analysis else state['query']}"),
        ("fiscal_periods", "Fiscal periods:\n" + ("\n".join(fiscal_periods) or "n/a")),
        ("step_results", f"Step results:\n{_step_details(state)}"),
        ("limits", "Known limits:\n" + ("\n".join(limits) or "none")),
        ("freshness", "Data freshness:\n" + ("\n".join(freshness) or "none")),
        ("sources", "Sources:\n" + ("\n\n".join(source_lines) or "(no sources were retrieved)")),
    ]


@dataclass(frozen=True)
class SourceFreshness:
    kind: str  # "news" | "8-K filings"
    ticker: str
    latest: date | None
    checked: datetime | None


def source_freshness(state: ResearchState) -> list[SourceFreshness]:
    """The tools' freshness notes, one per (source, company), newest check kept."""
    found: dict[tuple[str, str], SourceFreshness] = {}
    for result in state.get("step_results", {}).values():
        for m in FRESHNESS_NOTE_RE.finditer(result.data_digest):
            checked = None if m["checked"] == "never" else datetime.fromisoformat(m["checked"])
            item = SourceFreshness(
                m["kind"],
                m["ticker"],
                None if m["latest"] == "none" else date.fromisoformat(m["latest"]),
                checked,
            )
            old = found.get((item.kind, item.ticker))
            if old is None or (checked and (old.checked is None or checked > old.checked)):
                found[(item.kind, item.ticker)] = item
    return list(found.values())


def _day(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def freshness_lines(state: ResearchState) -> list[str]:
    """E.g. 'SEC filings checked 17 Nov 2026 (latest 8-K filed 12 Nov 2026)' and
    'news through 15 Nov 2026 (checked 17 Nov 2026)'."""
    items = source_freshness(state)
    several = len({f.ticker for f in items}) > 1
    lines = []
    for f in items:
        who = f"{f.ticker} " if several else ""
        checked = _day(f.checked.date()) if f.checked else "never"
        if f.kind == "8-K filings":
            latest = f"latest 8-K filed {_day(f.latest)}" if f.latest else "no 8-Ks stored"
            lines.append(f"{who}SEC filings checked {checked} ({latest})")
        else:
            latest = f"through {_day(f.latest)}" if f.latest else "none stored"
            lines.append(f"{who}news {latest} (checked {checked})")
    return lines


def freshness_footer(state: ResearchState) -> str:
    """The data line, plus a warning only when a source wasn't *checked* in the last two
    days. An old newest item alone is no warning: quiet companies file rarely (D-049)."""
    lines = freshness_lines(state)
    if not lines:
        return ""
    cutoff = date.fromisoformat(state["today"]) - timedelta(days=STALE_CHECK_DAYS)
    stale = [
        f"{'SEC filings' if f.kind == '8-K filings' else 'news'} for {f.ticker} "
        + (f"last checked {_day(f.checked.date())}" if f.checked else "never checked")
        for f in source_freshness(state)
        if f.checked is None or f.checked.date() < cutoff
    ]
    footer = "*Data: " + "; ".join(lines) + ".*"
    if stale:
        footer += (
            "\n*Freshness warning: " + "; ".join(stale) + "; very recent events may be missing.*"
        )
    return footer


def normalize_citation_style(answer: str) -> str:
    """gpt-oss native citations to [n]; other bracketed references (step ids) removed."""
    answer = _NATIVE_CITE_RE.sub(lambda m: f"[{m.group(1)}]", answer)
    return _NATIVE_OTHER_RE.sub("", answer)  # e.g. 【r1s1】: not a source number


# Gaps that only ask for more of what a step already retrieved (demo 7, 2026-11-04 and -05:
# "full headings ... the current output is truncated", "list of risk factors that were
# removed"). The full data is in the sources; replanning re-fetches it for nothing.
_DETAIL_GAP_RE = re.compile(
    r"truncat|\bfull (list|text|heading|wording|description|detail)|\bcomplete "
    r"(list|set|text|heading|description)|word[- ]by[- ]word|\ball (of the )?(\d+ )?"
    r"(risk factors?|headings?|items|changes)\b|\blist of (the )?(risk factors?|headings?|"
    r"items|changes) that (were|are|have|changed)|\beach of the \d+|\b(original|old|updated) "
    r"wording",
    re.I,
)


def drop_detail_gaps(critique: Critique) -> Critique:
    """Remove detail-only gaps; a critique left with none becomes "ok"."""
    kept = [g for g in critique.gaps if not _DETAIL_GAP_RE.search(g)]
    if len(kept) == len(critique.gaps):
        return critique
    dropped = len(critique.gaps) - len(kept)
    return critique.model_copy(
        update={
            "gaps": kept,
            "verdict": critique.verdict if kept else "ok",
            "notes": f"{critique.notes} ({dropped} detail-only gap(s) dropped: the data is "
            "already in the sources.)".strip(),
        }
    )


def resolve_citations(answer: str, state: ResearchState) -> tuple[str, list[Citation]]:
    """Map [n] to sources; remove references to numbers that don't exist."""
    answer = normalize_citation_style(answer)
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


def renumber_citations(
    answer: str, citations: list[Citation]
) -> tuple[str, list[Citation], dict[int, int]]:
    """Number the cited sources 1, 2, 3… in order of first appearance.

    The synthesizer cites from a numbered list of up to MAX_SOURCES, so a verified answer
    can read [4] [17] [2]. The reader sees 1, 2, 3; the Sources tab and the citations
    list use the same numbers. Runs after the citation check and resolve_citations
    (which work on the synthesizer's numbering), so every [n] left is a real source.
    Returns the mapping old -> new as well, so a client that streamed the draft in the
    old numbering can line the two up."""
    mapping: dict[int, int] = {}
    for m in _CITE_RE.finditer(answer):
        mapping.setdefault(int(m.group(1)), len(mapping) + 1)
    answer = _CITE_RE.sub(lambda m: f"[{mapping[int(m.group(1))]}]", answer)
    by_old = {c.n: c for c in citations}
    renumbered = [
        by_old[old].model_copy(update={"n": new})
        for old, new in sorted(mapping.items(), key=lambda kv: kv[1])
        if old in by_old
    ]
    return answer, renumbered, mapping


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
