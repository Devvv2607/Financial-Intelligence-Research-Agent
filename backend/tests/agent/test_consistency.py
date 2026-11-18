"""Answer consistency with the tools' own labels (D-051), on the two real failures of the
2026-11-17 replay recordings, plus the cases that must stay untouched."""

import dataclasses
import json
from pathlib import Path

from langchain_core.messages import HumanMessage

from agent.checkpoint import memory_saver
from agent.consistency import asserted_statuses, check_answer
from agent.graph import build_graph
from agent.tools.base import Evidence
from agent.tools.common import Company

from .fakes import (
    COMPANIES,
    OK_CRITIQUE,
    FakeLLM,
    FakeTools,
    analysis,
    make_deps,
    ok,
    step,
)

NBH = chr(0x2011)  # the non-breaking hyphen the model writes (re-worded, 364-day)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "consistency"


def load(name: str) -> tuple[str, list[Evidence]]:
    d = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return d["answer"], [Evidence.model_validate(s) for s in d["sources"]]


def risk(n: int, status: str | None) -> Evidence:
    return Evidence(
        source_id=f"rf:{n}",
        source_type="risk_factor",
        ticker="AAPL",
        title=f"AAPL 10-K risk factor {n}",
        snippet="text",
        compare_status=status,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- Apple (risk status)
def test_apple_unchanged_claims_about_changed_risks_are_removed() -> None:
    answer, sources = load("apple-risk-changes")
    fixed, report = check_answer(answer, sources)

    # The wrong part, as recorded: a lead line and four items calling changed risks unchanged.
    assert "remained unchanged" in answer and "identical language" in answer
    assert "remained unchanged" not in fixed
    assert "identical language" not in fixed
    for gone in ("[5][6]", "[7][8]", "[9][10]", "[11][12]"):
        assert gone not in fixed
    assert len(report.status_mismatches) == 4  # the four items; the lead line goes with them
    assert all("unchanged" in f.detail and "changed" in f.detail for f in report.status_mismatches)

    # The correct statements stay, untouched.
    for kept in (
        "added one new risk factor concerning artificial",
        f"re{NBH}worded the supply{NBH}chain risk",
        "1 new risk, 0 removed risks, 20 materially changed risks",
        "No risk factors were removed",
    ):
        assert kept in fixed
    assert report.summary()["status_mismatches"] == 4
    assert report.summary()["eight_k_rewrites"] == 0


def test_status_words_are_read_with_negations_and_typographic_hyphens() -> None:
    assert asserted_statuses("Cybersecurity risk (identical language) [9]") == {"unchanged"}
    assert asserted_statuses("was not changed between the filings") == {"unchanged"}
    assert asserted_statuses(f"Apple re{NBH}worded the supply risk") == {"changed"}
    assert asserted_statuses("The 10-K added one new risk factor on AI") == {"new"}
    assert asserted_statuses("The company added language about AI") == set()  # not a new risk
    assert asserted_statuses("Export controls remain a key risk") == set()


def test_matching_mixed_and_unlabeled_claims_are_kept() -> None:
    sources = [risk(1, "new"), risk(2, "changed"), risk(3, "unchanged"), risk(4, None)]
    answer = "\n".join(
        [
            "The FY2026 10-K added a new risk factor on AI [1].",
            "- The supply risk was expanded to compute capacity [2].",
            "| Developers | unchanged | [3] |",
            "A new AI risk sits beside the reworded supply risk [1][2].",
            "Apple lists 28 risk factors in total [4].",
        ]
    )
    fixed, report = check_answer(answer, sources)
    assert fixed == answer and report.status_mismatches == []


def test_reverse_mismatches_and_table_rows_are_removed() -> None:
    sources = [risk(1, "unchanged"), risk(2, "changed"), risk(3, "new")]
    answer = "\n".join(
        [
            "| Risk | Status | Source |",
            "| Developers | Reworded | [1] |",
            "| Supply | New risk factor | [2] |",
            "Intro sentence stays. The AI risk was removed [3]. Last sentence stays.",
        ]
    )
    fixed, report = check_answer(answer, sources)
    assert fixed.splitlines() == [
        "| Risk | Status | Source |",
        "Intro sentence stays. Last sentence stays.",
    ]
    assert len(report.status_mismatches) == 3


def test_answers_without_comparison_labels_are_not_touched() -> None:
    answer = "Supply chain risk was unchanged [1]."
    assert check_answer(answer, [risk(1, None)]) == (answer, check_answer(answer, [])[1])


# ---------------------------------------------------------------- Boeing (8-K wording)
def test_boeing_5_02_row_is_replaced_by_the_official_item_description() -> None:
    answer, sources = load("boeing-8k-events")
    fixed, report = check_answer(answer, sources)

    bad = "The Board reported the departure of certain officers and the appointment of new"
    assert bad in answer and bad not in fixed
    assert (
        "Reported under Item 5.02 (Departure or appointment of directors or officers; "
        "compensation arrangements) [3]" in fixed
    )
    assert len(report.eight_k_rewrites) == 1
    assert "depart" in report.eight_k_rewrites[0].detail
    assert "compensat" in report.eight_k_rewrites[0].detail

    # The other six rows describe what their excerpts say and stay as written.
    for kept in (
        "$3.0" + chr(0x202F) + f"billion, 364{NBH}day revolving credit agreement",
        "financial results for the third quarter of 2026",
        "disclosed the final voting results",
    ):
        assert kept in fixed
    assert fixed.count("\n") == answer.count("\n")  # same table shape


def test_an_8k_sentence_may_use_excerpt_words_and_quote_the_item_name() -> None:
    _, sources = load("boeing-8k-events")
    shedd = sources[2]  # [3]: the 21 Aug 2026 Item 5.02 filing
    ok = (
        "On 21 Aug 2026 Boeing filed an 8-K under Item 5.02 (Departure or appointment of "
        "directors or officers; compensation arrangements): the Board appointed Ryan L. Shedd "
        "as Senior Vice President and Controller [1]."
    )
    fixed, report = check_answer(ok, [shedd])
    assert fixed == ok and report.eight_k_rewrites == []

    bad = "On 21 Aug 2026 Boeing's CFO resigned [1]."
    fixed, report = check_answer(bad, [shedd])
    assert fixed.startswith("The 8-K filed 21 Aug 2026 reports Item 5.02 (Departure")
    assert fixed.endswith("[1].") and len(report.eight_k_rewrites) == 1


# ---------------------------------------------------------------- through the graph
async def test_graph_passes_labels_to_the_synthesizer_and_finalize_enforces_them() -> None:
    answer, sources = load("apple-risk-changes")
    labeled = [
        s.model_copy(update={"compare_note": f"{s.compare_status}; scripted"})
        if s.compare_status
        else s
        for s in sources
    ]
    q = "How did Apple's risk factors change between its last two 10-Ks?"
    tools = FakeTools(
        {"compare_risk_factors": ok("compare_risk_factors", {"counts": {"new": 1}}, labeled)}
    )
    plan = {
        "steps": [
            step(
                "a",
                "filings",
                entities=["AAPL"],
                tool_calls=[("compare_risk_factors", '{"ticker": "AAPL"}')],
            )
        ]
    }
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["Apple"])], "planner": [plan], "critic": [OK_CRITIQUE]},
        {"synthesizer": [answer]},
    )
    graph = build_graph(make_deps(llm, tools), memory_saver())
    out = await graph.ainvoke(
        {"messages": [HumanMessage(content=q)]},
        {"configurable": {"thread_id": "t", "agent_run_id": "r"}, "recursion_limit": 60},
    )

    synth_prompt = next(m for n, m in llm.calls if n == "synthesizer")[1].content
    assert "COMPARISON STATUS: CHANGED" in synth_prompt
    assert "Risk factor comparison (statuses decided by compare_risk_factors" in synth_prompt
    assert "identical language" not in out["answer"]
    assert "remained unchanged" not in out["answer"]
    assert "4 statement(s) removed" in out["answer"]
    assert out["citation_check"]["status_mismatches"] == 4
    # Citations were renumbered after the removal: no gaps.
    assert [c.n for c in out["citations"]] == list(range(1, len(out["citations"]) + 1))


async def test_graph_rewrites_the_boeing_item_title_narrative() -> None:
    answer, sources = load("boeing-8k-events")
    q = "What material events has Boeing reported in 8-Ks this year?"
    tools = FakeTools({"get_8k_events": ok("get_8k_events", {"events": [1]}, sources)})
    plan = {
        "steps": [
            step(
                "a",
                "filings",
                entities=["BA"],
                tool_calls=[("get_8k_events", '{"ticker": "BA", "since": "2026-01-01"}')],
            )
        ]
    }
    llm = FakeLLM(
        {"query_analyzer": [analysis(q, ["Boeing"])], "planner": [plan], "critic": [OK_CRITIQUE]},
        {"synthesizer": [answer]},
    )
    boeing = Company(9, "BA", "BOEING CO", "Industrials", "1231")

    async def companies() -> list[Company]:
        return [*COMPANIES, boeing]

    deps = dataclasses.replace(make_deps(llm, tools), list_companies=companies)
    graph = build_graph(deps, memory_saver())
    out = await graph.ainvoke(
        {"messages": [HumanMessage(content=q)]},
        {"configurable": {"thread_id": "t", "agent_run_id": "r"}, "recursion_limit": 60},
    )
    assert "appointment of new directors" not in out["answer"]
    assert "Reported under Item 5.02 (Departure or appointment" in out["answer"]
    assert "1 8-K description(s) replaced" in out["answer"]
    assert out["citation_check"]["eight_k_rewrites"] == 1
