"""Agent graph behaviour with scripted LLMs and fake tools (no Groq, no database).

Covers BUILD_SPEC 7 agent tests plus the owner's Phase 3 rules: parallel fan-out and
depends_on, failed dependencies -> skipped (nothing hangs), direct-call fallback, bounded
replans with unique ids, interrupt/resume returning to the node that asked, guardrail rule
+ analyzer advice intent, uncovered companies, follow-up memory, citation checking.
"""

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from agent.checkpoint import memory_saver
from agent.graph import RECURSION_LIMIT, build_graph
from agent.nodes.answer import (
    FOOTER,
    FOOTER_NO_SOURCES,
    MAX_PER_STEP,
    MAX_SOURCES,
    UNVERIFIED_NOTE,
    numbered_sources,
    resolve_citations,
    source_text,
)
from agent.state import PlanStep, StepResult

from .fakes import (
    OK_CRITIQUE,
    FakeLLM,
    FakeTools,
    analysis,
    ev,
    failing,
    make_deps,
    ok,
    step,
)
from .test_citecheck import bow_embed


def cfg(thread: str = "t1", run: str = "00000000-0000-0000-0000-000000000001") -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread, "agent_run_id": run},
        "recursion_limit": RECURSION_LIMIT,
    }


async def ask(graph: Any, question: str, thread: str = "t1") -> dict[str, Any]:
    return await graph.ainvoke({"messages": [HumanMessage(content=question)]}, cfg(thread))


def planner_returns(*steps_: dict[str, Any]) -> dict[str, Any]:
    return {"steps": list(steps_)}


# ---------------------------------------------------------------- guardrail / scope
async def test_advice_rule_refuses_without_any_llm_call() -> None:
    llm = FakeLLM()
    graph = build_graph(make_deps(llm, FakeTools({})), memory_saver())
    out = await ask(graph, "Should I buy Tesla stock now?")
    assert out["route"] == "advice"
    assert "can't give investment advice" in out["answer"] and "Inc.." not in out["answer"]
    assert out["answer"].endswith(FOOTER_NO_SOURCES) and llm.calls == []


async def test_analyzer_catches_advice_the_rule_misses() -> None:
    q = "Would NVDA make sense in my portfolio this year?"
    llm = FakeLLM({"query_analyzer": [analysis(q, ["NVDA"], advice_intent=True)]})
    graph = build_graph(make_deps(llm, FakeTools({})), memory_saver())
    out = await ask(graph, q)
    assert out["route"] == "advice" and llm.count("planner") == 0


async def test_uncovered_company_is_declined_with_close_matches() -> None:
    q = "What are Teslaa Motors' and Rivian's biggest risks?"
    llm = FakeLLM({"query_analyzer": [analysis(q, ["Rivian"])]})
    graph = build_graph(make_deps(llm, FakeTools({})), memory_saver())
    out = await ask(graph, "What are Rivian's biggest risks?")
    assert out["route"] == "uncovered"
    assert '"Rivian" is not in my dataset' in out["answer"]
    assert llm.count("planner") == 0  # never answered from model knowledge


# ---------------------------------------------------------------- plan execution
def _research_llm(q: str, plan: dict[str, Any], **chat: Any) -> FakeLLM:
    return FakeLLM(
        {
            "query_analyzer": [analysis(q, ["NVDA", "AMD"])],
            "planner": [plan],
            "critic": [OK_CRITIQUE],
        },
        {"synthesizer": ["NVDA flags export controls [1]; AMD flags supply limits [2]."], **chat},
    )


async def test_independent_steps_run_in_parallel_and_dependents_wait() -> None:
    q = "Compare NVDA and AMD risk factors and their revenue"
    plan = planner_returns(
        step(
            "a",
            "filings",
            entities=["NVDA"],
            tool_calls=[("get_risk_factors", '{"ticker": "NVDA"}')],
        ),
        step(
            "b", "filings", entities=["AMD"], tool_calls=[("get_risk_factors", '{"ticker": "AMD"}')]
        ),
        step("c", "financials", depends_on=["a", "b"]),
    )
    tools = FakeTools(
        {
            "get_risk_factors": ok("get_risk_factors", {"risk_factors": [1]}, []),
            "get_financial_metrics": ok("get_financial_metrics", {"companies": [1]}, [ev("f1")]),
        },
        delay=0.05,
    )

    async def risk_handler(args: dict[str, Any]) -> Any:
        return await ok(
            "get_risk_factors", {"risk_factors": [1]}, [ev(f"rf-{args['ticker']}", args["ticker"])]
        )(args)

    tools.handlers["get_risk_factors"] = risk_handler
    fin_call = AIMessage(
        content="",
        tool_calls=[
            {"name": "get_financial_metrics", "args": {"tickers": ["NVDA", "AMD"]}, "id": "c1"}
        ],
    )
    llm = _research_llm(q, plan, financials_agent=[fin_call, "Revenue grew [f1]."])
    graph = build_graph(make_deps(llm, tools), memory_saver())
    out = await ask(graph, q)

    (_, _, a_start, a_end), (_, _, b_start, b_end) = tools.log[0], tools.log[1]
    assert b_start < a_end and a_start < b_end  # a and b overlapped: same superstep
    c_start = next(s for name, _, s, _ in tools.log if name == "get_financial_metrics")
    assert c_start >= max(a_end, b_end)  # c waited for both dependencies
    results = out["step_results"]
    assert [results[k].mode for k in ("r1s1", "r1s2", "r1s3")] == ["direct", "direct", "agentic"]
    assert all(r.status == "done" for r in results.values())
    # the dependent step saw its dependencies' results
    fin_prompt = next(m for n, m in llm.calls if n == "financials_agent")
    assert "Result of r1s1" in fin_prompt[1].content and "Result of r1s2" in fin_prompt[1].content
    assert [c.n for c in out["citations"]] == [1, 2] and out["answer"].endswith(FOOTER)


async def test_failed_dependency_skips_dependents_and_answer_still_comes() -> None:
    q = "NVDA risks and what news says about them"
    plan = planner_returns(
        step("a", "filings", tool_calls=[("get_risk_factors", '{"ticker": "NVDA"}')]),
        step("b", "news", depends_on=["a"]),
        step("c", "news", depends_on=["b"]),
    )
    tools = FakeTools({"get_risk_factors": failing("get_risk_factors")})
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["NVDA"])], "planner": [plan], "critic": [OK_CRITIQUE]},
        {
            "filings_agent": ["I could not retrieve the risk factors."],
            "synthesizer": [
                "The risk factors could not be retrieved, so no comparison is possible."
            ],
        },
    )
    graph = build_graph(make_deps(llm, tools), memory_saver())
    out = await ask(graph, q)
    r = out["step_results"]
    assert r["r1s1"].status == "failed" and r["r1s1"].mode == "agentic"  # fell back, still failed
    assert r["r1s2"].status == "skipped" and "r1s1" in (r["r1s2"].skip_reason or "")
    assert r["r1s3"].status == "skipped" and "r1s2" in (r["r1s3"].skip_reason or "")
    synth_prompt = next(m for n, m in llm.calls if n == "synthesizer")[1].content
    assert "Not available: depends on r1s1" in synth_prompt  # the gap reaches the answer
    assert out["answer"].endswith(FOOTER_NO_SOURCES)  # nothing cited


async def test_empty_direct_call_falls_back_to_agentic_loop() -> None:
    q = "NVDA 8-K events this year"
    plan = planner_returns(
        step(
            "a",
            "filings",
            entities=["NVDA"],
            tool_calls=[("get_8k_events", '{"ticker": "NVDA", "since": "2026-01-01"}')],
        )
    )
    tools = FakeTools(
        {
            "get_8k_events": ok("get_8k_events", {"events": []}, []),
            "search_filings": ok("search_filings", {"hits": [1]}, [ev("8k-1")]),
        }
    )
    fallback = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "search_filings",
                "args": {"query": "NVDA 8-K", "tickers": ["NVDA"]},
                "id": "x",
            }
        ],
    )
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["NVDA"])], "planner": [plan], "critic": [OK_CRITIQUE]},
        {"filings_agent": [fallback, "Found one 8-K [1]."], "synthesizer": ["One event [1]."]},
    )
    out = await ask(build_graph(make_deps(llm, tools), memory_saver()), q)
    res = out["step_results"]["r1s1"]
    assert res.status == "done" and res.mode == "agentic"
    assert "fell back to agentic" in res.summary
    assert [t.tool for t in res.tool_calls] == ["get_8k_events", "search_filings"]


async def test_invalid_direct_call_is_dropped_by_the_planner() -> None:
    q = "AMD revenue"
    plan = planner_returns(
        step(
            "a",
            "financials",
            entities=["AMD"],
            tool_calls=[
                ("get_financial_metrics", '{"tickers": "not-a-list"}'),
                ("search_news", "{}"),
            ],
        )
    )
    tools = FakeTools(
        {"get_financial_metrics": ok("get_financial_metrics", {"c": [1]}, [ev("f", "AMD")])}
    )
    call = AIMessage(
        content="",
        tool_calls=[{"name": "get_financial_metrics", "args": {"tickers": ["AMD"]}, "id": "1"}],
    )
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["AMD"])], "planner": [plan], "critic": [OK_CRITIQUE]},
        {"financials_agent": [call, "Revenue [1]."], "synthesizer": ["Revenue [1]."]},
    )
    out = await ask(build_graph(make_deps(llm, tools), memory_saver()), q)
    assert out["plan"][0].tool_calls == []  # both planned calls invalid -> agentic step
    assert out["step_results"]["r1s1"].mode == "agentic"


async def test_critic_replans_at_most_twice_with_unique_ids_and_keeps_results() -> None:
    q = "NVDA risk factors"
    gaps = {"verdict": "gaps", "gaps": ["more detail"], "unanswerable": [], "notes": ""}
    planner_steps = [
        planner_returns(
            step(
                "s1",
                "filings",
                entities=["NVDA"],
                tool_calls=[("get_risk_factors", '{"ticker": "NVDA"}')],
            )
        )
        for _ in range(3)
    ]
    tools = FakeTools({"get_risk_factors": ok("get_risk_factors", {"x": [1]}, [ev("r")])})
    llm = FakeLLM(
        {
            "query_analyzer": [analysis(q, ["NVDA"])],
            "planner": planner_steps,
            "critic": lambda _m: gaps,
        },
        {"synthesizer": ["Done [1]."]},
    )
    out = await ask(build_graph(make_deps(llm, tools), memory_saver()), q)
    assert llm.count("planner") == 3 and llm.count("critic") == 3  # 1 plan + 2 replans
    assert [s.id for s in out["plan"]] == ["r1s1", "r2s1", "r3s1"]
    assert set(out["step_results"]) == {"r1s1", "r2s1", "r3s1"}  # earlier results kept
    assert out["replan_count"] == 2 and out["answer"]


# ---------------------------------------------------------------- clarification
async def test_analyzer_clarification_interrupts_and_resumes_at_the_analyzer() -> None:
    q = "How risky is the bank?"
    first = analysis(
        q,
        [],
        needs_clarification=True,
        clarification_question="Which bank, and which kind of risk?",
        clarification_options=["JPMorgan (JPM): credit risk", "JPMorgan: all risks"],
    )
    second = analysis("What are JPMorgan's credit risks?", ["JPMorgan"])
    plan = planner_returns(
        step(
            "a",
            "filings",
            entities=["JPM"],
            tool_calls=[
                ("get_risk_factors", '{"ticker": "JPM", "category": "financial_liquidity"}')
            ],
        )
    )
    tools = FakeTools({"get_risk_factors": ok("get_risk_factors", {"x": [1]}, [ev("j", "JPM")])})
    llm = FakeLLM(
        {"query_analyzer": [first, second], "planner": [plan], "critic": [OK_CRITIQUE]},
        {"synthesizer": ["JPMorgan credit risk [1]."]},
    )
    graph = build_graph(make_deps(llm, tools), memory_saver())
    paused = await ask(graph, q, thread="clar")
    intr = paused["__interrupt__"][0].value
    assert intr["source"] == "query_analyzer" and len(intr["options"]) == 2
    assert llm.count("planner") == 0

    done = await graph.ainvoke(Command(resume="JPMorgan (JPM): credit risk"), cfg("clar"))
    assert llm.count("query_analyzer") == 2  # resumed at the analyzer, not the start
    assert done["turn"] == 1 and done["clarification"].answer.startswith("JPMorgan")
    second_prompt = [m for n, m in llm.calls if n == "query_analyzer"][1][1]["content"]
    assert "The user answered" in second_prompt
    assert done["step_results"]["r1s1"].status == "done"


async def test_resolver_clarification_resumes_at_the_resolver() -> None:
    q = "How risky is Johnson?"
    plan = planner_returns(
        step(
            "a", "filings", entities=["JNJ"], tool_calls=[("get_risk_factors", '{"ticker": "JNJ"}')]
        )
    )
    tools = FakeTools({"get_risk_factors": ok("get_risk_factors", {"x": [1]}, [ev("j", "JNJ")])})
    llm = FakeLLM(
        {
            "query_analyzer": [analysis(q, ["Johnson & Johnson"])],
            "planner": [plan],
            "critic": [OK_CRITIQUE],
        },
        {"synthesizer": ["JNJ risks [1]."]},
    )
    graph = build_graph(make_deps(llm, tools), memory_saver())
    paused = await ask(graph, q, thread="res")
    intr = paused["__interrupt__"][0].value
    assert intr["source"] == "entity_resolver"
    assert llm.calls == []  # the resolver asked before any LLM call

    done = await graph.ainvoke(Command(resume="JOHNSON & JOHNSON (JNJ)"), cfg("res"))
    assert [e.ticker for e in done["resolved_entities"]] == ["JNJ"]
    assert llm.count("query_analyzer") == 1


# ---------------------------------------------------------------- memory + citations
async def test_follow_up_uses_conversation_and_resets_evidence() -> None:
    q1, q2 = "What are NVDA's top risk factors?", "Now do the same for Intel."
    plans = [
        planner_returns(
            step(
                "a",
                "filings",
                entities=[t],
                tool_calls=[("get_risk_factors", json.dumps({"ticker": t}))],
            )
        )
        for t in ("NVDA", "INTC")
    ]

    async def rf(args: dict[str, Any]) -> Any:
        return await ok(
            "get_risk_factors", {"x": [1]}, [ev(f"rf-{args['ticker']}", args["ticker"])]
        )(args)

    tools = FakeTools({"get_risk_factors": rf})
    llm = FakeLLM(
        {
            "query_analyzer": [
                analysis(q1, ["NVDA"]),
                analysis("What are Intel's top risk factors?", ["Intel"]),
            ],
            "planner": plans,
            "critic": [OK_CRITIQUE],
        },
        {"synthesizer": ["Risks [1]."]},
    )
    graph = build_graph(make_deps(llm, tools), memory_saver())
    await ask(graph, q1, thread="mem")
    second = await ask(graph, q2, thread="mem")
    second_prompt = [m for n, m in llm.calls if n == "query_analyzer"][1][1]["content"]
    assert q1 in second_prompt and "Risks" in second_prompt  # previous Q and A are visible
    assert [e.source_id for e in second["evidence"]] == ["rf-INTC"]  # turn-1 evidence reset
    assert second["turn"] == 2 and len(second["messages"]) == 4


async def test_unknown_citation_numbers_are_removed() -> None:
    q = "NVDA risks"
    plan = planner_returns(
        step("a", "filings", tool_calls=[("get_risk_factors", '{"ticker": "NVDA"}')])
    )
    tools = FakeTools({"get_risk_factors": ok("get_risk_factors", {"x": [1]}, [ev("only")])})
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["NVDA"])], "planner": [plan], "critic": [OK_CRITIQUE]},
        {"synthesizer": ["Export controls [1]. Made-up claim [7]."]},
    )
    recorded: list[dict[str, Any]] = []
    out = await ask(build_graph(make_deps(llm, tools, recorded), memory_saver()), q)
    assert "[7]" not in out["answer"] and "[1]" in out["answer"]
    assert [c.source_id for c in out["citations"]] == ["only"]
    assert recorded and recorded[0]["plan"][0]["id"] == "r1s1"


def test_no_request_can_reach_groq() -> None:
    """The autouse guard turns any call to api.groq.com into a failure."""
    with pytest.raises(AssertionError, match="Groq"):
        httpx.get("https://api.groq.com/openai/v1/models")


def test_native_citations_are_normalised() -> None:
    """gpt-oss sometimes cites 【2†source】 or a step id; only real source numbers survive."""
    state: Any = {"evidence_order": ["a", "b"], "evidence": [ev("a"), ev("b")]}
    text, cites = resolve_citations("Risk X【2†source】. Y【r1s1】 [7].", state)
    assert text == "Risk X[2]. Y ."
    assert [c.n for c in cites] == [2]


def test_sources_are_shared_across_steps() -> None:
    """One step with many sources must not crowd the others out of the [n] numbering."""
    big = [ev(f"amd{i}", "AMD") for i in range(50)]
    small = [ev("nvda0"), ev("news0")]
    plan = [PlanStep.model_validate(step(i, "filings")) for i in ("r1s1", "r1s2", "r1s3")]
    ids = [[e.source_id for e in big], ["nvda0"], ["news0"]]
    results = {
        p.id: StepResult(step_id=p.id, status="done", mode="direct", evidence_ids=i)
        for p, i in zip(plan, ids, strict=True)
    }
    state: Any = {"plan": plan, "step_results": results, "evidence": big + small}
    order = [e.source_id for e in numbered_sources(state)]
    assert len(order) == MAX_PER_STEP + 2 <= MAX_SOURCES  # the big step keeps its top items
    assert order[:4] == ["amd0", "nvda0", "news0", "amd1"]
    assert order[-1] == f"amd{MAX_PER_STEP - 1}"


def test_source_text_drops_repeated_heading_and_cuts_at_a_word() -> None:
    e = ev("rf1").model_copy(
        update={
            "title": "AMD 10-K FY2025 risk factor: The markets are highly competitive.",
            "snippet": "The markets are highly competitive. " + "Rivals ship faster parts " * 20,
        }
    )
    text = source_text(e, limit=40)
    assert text.startswith("Rivals ship") and text.endswith("…")
    assert len(text) <= 41


async def test_finalize_verifies_citations_and_records_precision() -> None:
    q = "NVDA risks"
    plan = planner_returns(
        step("a", "filings", tool_calls=[("get_risk_factors", '{"ticker": "NVDA"}')])
    )
    export = ev("rf-export").model_copy(
        update={"title": "NVDA risk factor", "snippet": "Export controls restrict sales to China."}
    )
    tools = FakeTools({"get_risk_factors": ok("get_risk_factors", {"x": [1]}, [export])})
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["NVDA"])], "planner": [plan], "critic": [OK_CRITIQUE]},
        {
            "synthesizer": [
                "Export controls restrict NVIDIA sales to China [1]. "
                "Quarterly revenue doubled on gaming demand [1]."
            ]
        },
    )
    recorded: list[dict[str, Any]] = []
    deps = make_deps(llm, tools, recorded)
    deps.embed = bow_embed
    out = await ask(build_graph(deps, memory_saver()), q)
    assert "China [1]." in out["answer"]
    assert "gaming demand [unverified]." in out["answer"]
    assert out["answer"].endswith(FOOTER + UNVERIFIED_NOTE)
    check = out["citation_check"]
    assert (check["checked"], check["supported"], check["unverified_claims"]) == (2, 1, 1)
    assert recorded[0]["citation_check"] == check


async def test_planner_sees_categories_and_a_category_filter_reaches_the_tool() -> None:
    """Demo 3 fetched every UNH risk factor for a 'regulatory risks' question; the planner
    is now told to filter, sees the valid names, and a bad name never reaches the tool."""
    q = "What are UNH's main regulatory risks?"
    plan = planner_returns(
        step(
            "a",
            "filings",
            entities=["JNJ"],
            tool_calls=[("get_risk_factors", '{"ticker": "JNJ", "category": "regulatory"}')],
        ),
        step(
            "b",
            "filings",
            entities=["JNJ"],
            tool_calls=[("get_risk_factors", '{"ticker": "JNJ", "category": "regulation"}')],
        ),
    )
    tools = FakeTools({"get_risk_factors": ok("get_risk_factors", {"x": [1]}, [ev("r", "JNJ")])})
    llm = FakeLLM(
        {
            "query_analyzer": [analysis(q, ["Johnson & Johnson"])],
            "planner": [plan],
            "critic": [OK_CRITIQUE],
        },
        {"filings_agent": ["nothing more"], "synthesizer": ["Regulatory [1]."]},
    )
    out = await ask(build_graph(make_deps(llm, tools), memory_saver()), q)
    planner_prompt = next(m for n, m in llm.calls if n == "planner")[0]["content"]
    assert "pass get_risk_factors the matching `category`" in planner_prompt
    assert "category?: regulatory|competition|supply_chain" in planner_prompt
    assert ("get_risk_factors", {"ticker": "JNJ", "category": "regulatory"}) in [
        (name, args) for name, args, *_ in tools.log
    ]
    assert out["plan"][1].tool_calls == []  # "regulation" is not a category: dropped


async def test_identical_tool_calls_in_a_step_run_once() -> None:
    """Demo 2 (2026-11-05): the same search_filings call three times in one step."""
    q = "AAPL 10-Ks"
    plan = planner_returns(step("a", "filings", entities=["AAPL"]))  # agentic
    same = {"query": "Apple", "tickers": ["AAPL"], "form_types": ["10-K"]}

    def call(i: int) -> AIMessage:
        return AIMessage(
            content="", tool_calls=[{"name": "search_filings", "args": same, "id": f"c{i}"}]
        )

    tools = FakeTools({"search_filings": ok("search_filings", {"hits": [1]}, [ev("f", "AAPL")])})
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["Apple"])], "planner": [plan], "critic": [OK_CRITIQUE]},
        {
            "filings_agent": [call(1), call(2), call(3), "Two filings [1]."],
            "synthesizer": ["ok [1]."],
        },
    )
    out = await ask(build_graph(make_deps(llm, tools), memory_saver()), q)
    assert [name for name, *_ in tools.log] == ["search_filings"]  # executed once
    r = out["step_results"]["r1s1"]
    assert [t.reused for t in r.tool_calls] == [False, True, True]
    assert r.status == "done"
    last_turn = [m for n, m in llm.calls if n == "filings_agent"][-1]
    assert "result reused" in str(last_turn[-1].content)


async def test_agentic_fallback_does_not_rerun_a_failed_direct_call() -> None:
    q = "AMD risks"
    args = '{"ticker": "AMD"}'
    plan = planner_returns(
        step("a", "filings", entities=["AMD"], tool_calls=[("get_risk_factors", args)])
    )
    retry = AIMessage(
        content="",
        tool_calls=[{"name": "get_risk_factors", "args": {"ticker": "AMD"}, "id": "c1"}],
    )
    tools = FakeTools({"get_risk_factors": failing("get_risk_factors")})
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["AMD"])], "planner": [plan], "critic": [OK_CRITIQUE]},
        {"filings_agent": [retry, "Could not retrieve them."], "synthesizer": ["None."]},
    )
    out = await ask(build_graph(make_deps(llm, tools), memory_saver()), q)
    assert len(tools.log) == 1
    r = out["step_results"]["r1s1"]
    assert r.status == "failed"  # a reused failure doesn't make the step look ok
    assert [t.reused for t in r.tool_calls] == [False, True]


async def test_demo7_detail_only_gaps_do_not_replan() -> None:
    """Demo 7 (2026-11-04 and -05): the comparison was done, but the critic asked for full
    lists 'because the output is truncated' and replanned to re-fetch the same data."""
    q = "Now do the same for Intel."
    plan = planner_returns(
        step(
            "a",
            "filings",
            entities=["INTC"],
            tool_calls=[("compare_risk_factors", '{"ticker": "INTC"}')],
        )
    )
    critic_says = {
        "verdict": "gaps",
        "gaps": [
            "list of risk factors that were removed between 2024 and 2025 10-K",
            "list of risk factors that changed (with brief description) between 2024 and 2025",
            "full headings for the new risk factors (the current output is truncated)",
        ],
        "unanswerable": [],
        "notes": "need more detail",
    }
    tools = FakeTools(
        {
            "compare_risk_factors": ok(
                "compare_risk_factors", {"counts": {"new": 7}}, [ev("cmp", "INTC")]
            )
        }
    )
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["Intel"])], "planner": [plan], "critic": [critic_says]},
        {"synthesizer": ["Intel added 7 risk factors [1]."]},
    )
    out = await ask(build_graph(make_deps(llm, tools), memory_saver()), q)
    assert llm.count("planner") == 1 and llm.count("critic") == 1  # no replan round
    assert out["critique"].verdict == "ok" and out["critique"].gaps == []
    assert "3 detail-only gap(s) dropped" in out["critique"].notes
    critic_prompt = next(m for n, m in llm.calls if n == "critic")[0]["content"]
    assert "intentionally shortened summaries" in critic_prompt
