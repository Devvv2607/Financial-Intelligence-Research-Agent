"""Interrupt on one graph instance, resume on a fresh one: the pause lives in Postgres.
Scripted LLM and fake tools (no Groq); skipped when the local Postgres isn't reachable."""

import uuid

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from agent.checkpoint import postgres_saver
from agent.graph import RECURSION_LIMIT, build_graph
from core.config import Settings

from .fakes import OK_CRITIQUE, FakeLLM, FakeTools, analysis, ev, make_deps, ok, step

pytestmark = pytest.mark.integration


async def test_clarification_survives_a_restart_via_postgres() -> None:
    settings = Settings()
    q = "How risky is the bank?"
    llm = FakeLLM(
        {
            "query_analyzer": [
                analysis(
                    q,
                    [],
                    needs_clarification=True,
                    clarification_question="Which bank?",
                    clarification_options=["JPMorgan (JPM)", "Another bank"],
                ),
                analysis("What are JPMorgan's risks?", ["JPMorgan"]),
            ],
            "planner": [
                {
                    "steps": [
                        step(
                            "a",
                            "filings",
                            entities=["JPM"],
                            tool_calls=[("get_risk_factors", '{"ticker": "JPM"}')],
                        )
                    ]
                }
            ],
            "critic": [OK_CRITIQUE],
        },
        {"synthesizer": ["JPM risks [1]."]},
    )
    tools = FakeTools({"get_risk_factors": ok("get_risk_factors", {"x": [1]}, [ev("j", "JPM")])})
    config = {
        "configurable": {"thread_id": f"test-{uuid.uuid4()}", "agent_run_id": str(uuid.uuid4())},
        "recursion_limit": RECURSION_LIMIT,
    }
    try:
        async with postgres_saver(settings.database_url) as saver:
            paused = await build_graph(make_deps(llm, tools), saver).ainvoke(
                {"messages": [HumanMessage(content=q)]}, config
            )
    except OSError as exc:  # pragma: no cover - stack not running
        pytest.skip(f"Postgres not reachable: {exc}")
    assert paused["__interrupt__"][0].value["question"] == "Which bank?"

    async with postgres_saver(settings.database_url) as fresh:  # new connection + graph
        done = await build_graph(make_deps(llm, tools), fresh).ainvoke(
            Command(resume="JPMorgan (JPM)"), config
        )
    assert done["step_results"]["r1s1"].status == "done"
    assert done["answer"].startswith("JPM risks [1].")
