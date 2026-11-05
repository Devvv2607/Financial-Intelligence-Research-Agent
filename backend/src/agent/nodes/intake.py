"""Intake nodes: start_turn, guardrail, entity_resolver, query_analyzer, ask_clarification.

Interrupt rule (DECISIONS D-005): a node that calls interrupt() is re-run from the top
on resume, so `ask_clarification` only reads the question already stored in state and
interrupts. The LLM work that produced the question happened in query_analyzer (or the
deterministic resolver) and is checkpointed before the interrupt.
"""

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.types import interrupt
from rapidfuzz import fuzz, process

from agent import prompts
from agent.deps import AgentDeps
from agent.state import RESET, Analysis, Clarification, Entity, ResearchState, Uncovered
from agent.tools.common import Company
from agent.tools.news_relevance import name_aliases
from core.llm import DailyLimitError, LLMError

MAX_CLARIFICATIONS = 2
FUZZY_ACCEPT = 90  # rapidfuzz WRatio: accept a misspelled/variant company name
FUZZY_SUGGEST = 60  # ...or list it as a close match for an uncovered company

_ADVICE_RE = re.compile(
    r"\b(should|shall|would|do you think) (i|we)\b.{0,40}\b(buy|sell|short|hold|invest|"
    r"dump|own)\b|\bis (it )?a (good|bad|smart) (time to (buy|sell)|buy|sell|investment)\b"
    r"|\b(price target|buy or sell|worth (buying|investing))\b",
    re.I,
)


def run_id_of(config: RunnableConfig) -> str | None:
    # Not `configurable.run_id`: LangGraph treats that key as the run's identity and a
    # repeated value makes a new turn on the thread a no-op (found in testing, D-033).
    return (config.get("configurable") or {}).get("agent_run_id")


def _emit(event: str, **data: Any) -> None:
    get_stream_writer()({"event": event, **data})


def make_intake_nodes(deps: AgentDeps) -> dict[str, Any]:
    async def start_turn(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
        last_human = next(
            (m for m in reversed(state.get("messages", [])) if isinstance(m, HumanMessage)), None
        )
        return {
            "turn": state.get("turn", 0) + 1,
            "run_id": run_id_of(config) or "",
            "today": deps.today().isoformat(),
            "query": str(last_human.content) if last_human else "",
            "route": None,
            "resolved_entities": [],
            "uncovered": [],
            "clarification": None,
            "clarification_count": 0,
            "analysis": None,
            "plan": [],
            "plan_round": 0,
            "step_results": RESET,
            "evidence": RESET,
            "dispatch_rounds": 0,
            "critique": None,
            "replan_count": 0,
            "answer": None,
            "evidence_order": [],
            "citations": [],
            "citation_check": None,
        }

    async def guardrail(state: ResearchState) -> dict[str, Any]:
        """Cheap rule first: obvious advice requests never reach an LLM."""
        if _ADVICE_RE.search(state["query"]):
            matches = match_companies(state["query"], await deps.companies())
            return {"route": "advice", "answer": advice_answer(matches)}
        return {"route": "research"}

    async def entity_resolver(state: ResearchState) -> dict[str, Any]:
        """Deterministic: match names/aliases/tickers against the ingested companies.

        Asks (interrupt via ask_clarification) when one mention fits several companies,
        e.g. "Johnson" -> Johnson & Johnson or Johnson Controls. On resume, the run comes
        back here and keeps only the company the user picked.
        """
        clar = state.get("clarification")
        answered = clar is not None and clar.source == "entity_resolver" and clar.answer
        companies = await deps.companies()
        entities = match_companies(state["query"], companies)
        if answered:
            entities = _apply_choice(entities, str(clar.answer), companies)  # type: ignore[union-attr]
            return {"resolved_entities": entities, "clarification": clar}
        ambiguous = _shared_alias(entities) or _ambiguous_mention(
            state["query"], companies, entities
        )
        if ambiguous and state.get("clarification_count", 0) < MAX_CLARIFICATIONS:
            mention, options = ambiguous
            return {
                "resolved_entities": entities,
                "clarification": Clarification(
                    source="entity_resolver",
                    question=f'Which company do you mean by "{mention}"?',
                    options=options,
                ),
            }
        return {"resolved_entities": entities, "clarification": clar}

    async def query_analyzer(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
        companies = await deps.companies()
        clar = state.get("clarification")
        user = _analyzer_input(state, companies)
        try:
            analysis = await deps.llm.structured(
                "query_analyzer",
                Analysis,
                [
                    {"role": "system", "content": prompts.load("query_analyzer")},
                    {"role": "user", "content": user},
                ],
                run_id=run_id_of(config),
            )
        except (DailyLimitError, LLMError) as exc:
            return {"route": "error", "answer": f"I couldn't analyse the question: {exc}"}

        if analysis.advice_intent:
            return {
                "analysis": analysis,
                "route": "advice",
                "answer": advice_answer(state.get("resolved_entities", [])),
            }

        entities = list(state.get("resolved_entities", []))
        uncovered = _resolve_mentions(analysis.company_mentions, companies, entities)
        update: dict[str, Any] = {
            "analysis": analysis,
            "resolved_entities": entities,
            "uncovered": uncovered,
        }
        already_answered = clar is not None and clar.source == "query_analyzer" and clar.answer
        if (
            analysis.needs_clarification
            and len(analysis.clarification_options) >= 2
            and not already_answered
            and state.get("clarification_count", 0) < MAX_CLARIFICATIONS
        ):
            update["clarification"] = Clarification(
                source="query_analyzer",
                question=analysis.clarification_question,
                options=analysis.clarification_options[:4],
            )
            return update
        if uncovered and not entities and analysis.company_mentions:
            update["route"] = "uncovered"
            update["answer"] = uncovered_answer(uncovered, companies)
        return update

    async def ask_clarification(state: ResearchState) -> dict[str, Any]:
        clar = state["clarification"]
        assert clar is not None
        _emit("clarification_needed", question=clar.question, options=clar.options)
        answer = interrupt(
            {"question": clar.question, "options": clar.options, "source": clar.source}
        )
        return {
            "clarification": clar.model_copy(update={"answer": str(answer)}),
            "clarification_count": state.get("clarification_count", 0) + 1,
            "messages": [AIMessage(content=clar.question), HumanMessage(content=str(answer))],
        }

    return {
        "start_turn": start_turn,
        "guardrail": guardrail,
        "entity_resolver": entity_resolver,
        "query_analyzer": query_analyzer,
        "ask_clarification": ask_clarification,
    }


# ---------------------------------------------------------------- matching helpers
def _ticker_in(text: str, ticker: str) -> bool:
    # Tickers must be written in capitals; 1-letter ones (F, T, C) only as $F to avoid
    # matching ordinary words.
    if len(ticker) == 1:
        return re.search(rf"\${re.escape(ticker)}\b", text) is not None
    return re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", text) is not None


def match_companies(text: str, companies: list[Company]) -> list[Entity]:
    found: list[Entity] = []
    for c in companies:
        alias = next(
            (
                a
                for a in name_aliases(c.name)
                if re.search(r"\b" + r"[\s-]+".join(map(re.escape, a.split())) + r"\b", text, re.I)
            ),
            None,
        )
        if alias is not None:
            found.append(
                Entity(ticker=c.ticker, cik=c.cik, name=c.name, confidence=1.0, matched_on=alias)
            )
        elif _ticker_in(text, c.ticker):
            found.append(
                Entity(ticker=c.ticker, cik=c.cik, name=c.name, confidence=1.0, matched_on=c.ticker)
            )
    return found


def _shared_alias(entities: list[Entity]) -> tuple[str, list[str]] | None:
    """One matched word that names 2+ companies ("Johnson")."""
    groups: dict[str, list[Entity]] = {}
    for e in entities:
        groups.setdefault(e.matched_on.lower(), []).append(e)
    for mention, group in groups.items():
        if len(group) > 1:
            return mention.title(), [f"{e.name} ({e.ticker})" for e in group]
    return None


def _apply_choice(entities: list[Entity], answer: str, companies: list[Company]) -> list[Entity]:
    """Keep the companies the user's answer names; drop the other candidates of the
    ambiguous mention. An answer naming a company not yet matched adds it."""
    # Only the full name or the ticker counts here: the shared first word ("Johnson") is
    # exactly what was ambiguous, and every option repeats it.
    chosen = [
        e
        for e in match_companies(answer, companies)
        if e.matched_on == e.ticker or e.matched_on == name_aliases(e.name)[0]
    ]
    chosen_tickers = {e.ticker for e in chosen}
    shared = _shared_alias(entities)
    ambiguous = {e.matched_on.lower() for e in entities} if shared else set()
    if shared:
        ambiguous = {shared[0].lower()}
    kept = [
        e for e in entities if e.matched_on.lower() not in ambiguous or e.ticker in chosen_tickers
    ]
    kept += [e for e in chosen if e.ticker not in {k.ticker for k in kept}]
    return kept


def _candidate_phrases(text: str) -> list[str]:
    """Capitalised word runs ("Exxon Mobil", "Johnson") that may name a company."""
    return re.findall(r"\b[A-Z][a-zA-Z&.'-]+(?:\s+[A-Z][a-zA-Z&.'-]+)*", text)


def _ambiguous_mention(
    text: str, companies: list[Company], entities: list[Entity]
) -> tuple[str, list[str]] | None:
    """A capitalised phrase (not already matched) that is a near-spelling of 2+ covered
    companies' names about equally. Strict `ratio` on short names, not WRatio: partial
    matching made "Intel" look like "Johnson Controls *Intel*national"."""
    known = {e.ticker for e in entities}
    matched = {e.matched_on.lower() for e in entities}
    choices = {
        c.ticker: (name_aliases(c.name) or [c.name])[0] for c in companies if c.ticker not in known
    }
    for phrase in _candidate_phrases(text):
        if len(phrase) < 4 or phrase.lower() in matched:
            continue
        hits = process.extract(phrase, choices, scorer=fuzz.ratio, limit=3)
        strong = [h for h in hits if h[1] >= FUZZY_ACCEPT]
        if len(strong) >= 2 and strong[0][1] - strong[1][1] <= 3:
            return phrase, [f"{h[2]} ({h[0]})" for h in strong]
    return None


def _resolve_mentions(
    mentions: list[str], companies: list[Company], entities: list[Entity]
) -> list[Uncovered]:
    """Add mentions the deterministic pass missed; return the ones not covered at all."""
    by_name = {c.ticker: c for c in companies}
    names = {c.ticker: c.name for c in companies}
    uncovered: list[Uncovered] = []
    for mention in mentions:
        if any(match_companies(mention, [by_name[e.ticker]]) for e in entities):
            continue  # already resolved
        direct = match_companies(mention, companies)
        if direct:
            entities.extend(e for e in direct if e.ticker not in {x.ticker for x in entities})
            continue
        best = process.extract(mention, names, scorer=fuzz.WRatio, limit=3)
        if best and best[0][1] >= FUZZY_ACCEPT:
            c = by_name[best[0][2]]
            entities.append(
                Entity(
                    ticker=c.ticker,
                    cik=c.cik,
                    name=c.name,
                    confidence=best[0][1] / 100,
                    matched_on=mention,
                )
            )
            continue
        uncovered.append(
            Uncovered(
                mention=mention,
                close_matches=[f"{h[0]} ({h[2]})" for h in best if h[1] >= FUZZY_SUGGEST],
            )
        )
    return uncovered


def _analyzer_input(state: ResearchState, companies: list[Company]) -> str:
    history = []
    for m in state.get("messages", [])[:-1][-6:]:  # previous turns, newest last
        role = "User" if isinstance(m, HumanMessage) else "Assistant"
        history.append(f"{role}: {str(m.content)[:600]}")
    clar = state.get("clarification")
    lines = [
        f"Today's date: {state['today']}",
        "Conversation so far:\n" + ("\n".join(history) if history else "(none)"),
        f"Question: {state['query']}",
    ]
    if clar and clar.answer:
        lines.append(f'You asked: "{clar.question}" The user answered: "{clar.answer}"')
    resolved = ", ".join(f"{e.ticker} ({e.name})" for e in state.get("resolved_entities", []))
    lines.append(f"Companies already recognised: {resolved or 'none'}")
    lines.append(
        "Covered companies: "
        + "; ".join(
            f"{c.ticker} {name_aliases(c.name)[0] if name_aliases(c.name) else c.name}"
            for c in companies
        )
    )
    return "\n\n".join(lines)


# ---------------------------------------------------------------- fixed answers
def advice_answer(entities: list[Entity]) -> str:
    names = ", ".join(e.name.rstrip(".") for e in entities) or "a company"
    example = entities[0].ticker if entities else "NVDA"
    return (
        f"I can't give investment advice, so I won't say whether to buy, sell or hold "
        f"{names}. What I can do is research it from SEC filings and recent news: its main "
        f"risk factors, how they changed year over year, recent 8-K events, and reported "
        f"financials. For example, ask: \"What are {example}'s main risk factors and how "
        f'did its revenue change last year?"'
    )


def uncovered_answer(uncovered: list[Uncovered], companies: list[Company]) -> str:
    lines = []
    for u in uncovered:
        close = (
            f" Close matches I do cover: {', '.join(u.close_matches)}." if u.close_matches else ""
        )
        lines.append(f'- "{u.mention}" is not in my dataset, so I can\'t answer about it.{close}')
    return (
        "I only answer from the SEC filings and news I have ingested, and I won't answer "
        "about companies outside that set from general knowledge.\n\n"
        + "\n".join(lines)
        + f"\n\nI currently cover {len(companies)} companies, e.g. "
        + ", ".join(c.ticker for c in companies[:12])
        + (" and more." if len(companies) > 12 else ".")
    )
