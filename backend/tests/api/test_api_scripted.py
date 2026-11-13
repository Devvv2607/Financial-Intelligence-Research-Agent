"""AGENT_RUNTIME=scripted: every E2E scenario over HTTP, zero tokens (no runtime injected:
create_app picks the scripted runtime from settings, as the E2E stack will)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from api import scripted
from api.main import create_app
from core.config import Settings

from .test_api_agent import events, first, kinds


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(scripted, "TOOL_DELAY_S", 0.0)  # realism delays off in tests
    monkeypatch.setattr(scripted, "TOKEN_DELAY_S", 0.0)
    settings = Settings(
        _env_file=None,
        app_env="test",
        agent_runtime="scripted",
        groq_api_key=SecretStr("gsk_unused"),
        api_runs_per_minute=100,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c,
    ):
        yield c


async def ask(c: httpx.AsyncClient, q: str) -> tuple[str, list[dict[str, Any]]]:
    thread = (await c.post("/api/threads", json={})).json()["id"]
    resp = await c.post(f"/api/threads/{thread}/runs", json={"query": q})
    return thread, events(resp)


async def resume(c: httpx.AsyncClient, thread: str, answer: str) -> list[dict[str, Any]]:
    return events(await c.post(f"/api/threads/{thread}/resume", json={"answer": answer}))


async def test_compare_runs_parallel_and_flags_the_unsupported_claim(
    client: httpx.AsyncClient,
) -> None:
    _, evs = await ask(
        client,
        "Compare the top risk factors NVIDIA and AMD disclosed in their latest 10-Ks. "
        "Does recent news support any of them?",
    )
    started = [e for e in evs if e["event"] == "step_started"]
    assert len(started) == 4 and {e["mode"] for e in started} == {"direct"}
    final = first(evs, "final")
    streamed = "".join(e["text"] for e in evs if e["event"] == "token")
    # The verified answer differs from the streamed preview: the UI swaps them.
    assert "[unverified]" in final["answer"] and "[unverified]" not in streamed
    check = final["citation_check"]
    assert check["unverified_claims"] == 1 and check["supported"] >= 5
    types = {c["source_id"].split("-")[0] for c in final["citations"]}
    assert {"scripted:rf", "scripted:news"} <= types
    assert all("(scripted)" in c["title"] for c in final["citations"])
    assert "Freshness warning" in final["answer"]


async def test_recent_event_answer_discloses_stale_source_data(
    client: httpx.AsyncClient,
) -> None:
    _, evs = await ask(client, "Did NVIDIA's CEO resign on 12 November 2026?")
    final = first(evs, "final")
    assert "data through 2026-11-08" in final["answer"]
    assert "Freshness warning" in final["answer"]


async def test_apple_replans_with_an_agentic_step(client: httpx.AsyncClient) -> None:
    _, evs = await ask(client, "How did Apple's risk factors change between its last two 10-Ks?")
    assert kinds(evs).count("critique") == 2
    replan = first(evs, "replan")
    assert replan["round"] == 2 and replan["steps"][0]["id"] == "r2s1"
    agentic = [e for e in evs if e["event"] == "step_started" and e["step_id"] == "r2s1"]
    assert agentic[0]["mode"] == "agentic"
    tool = next(e for e in evs if e["event"] == "tool_called" and e["step_id"] == "r2s1")
    assert tool["tool"] == "search_news" and tool["ok"]
    assert "[unverified]" not in first(evs, "final")["answer"]


async def test_bank_asks_then_answers_after_resume(client: httpx.AsyncClient) -> None:
    thread, evs = await ask(client, "How risky is the bank?")
    asked = first(evs, "clarification_needed")
    assert asked["source"] == "query_analyzer" and "JPMorgan Chase (JPM)" in asked["options"]
    after = await resume(client, thread, "JPMorgan Chase (JPM)")
    assert kinds(after)[-1] == "final"
    assert "JPMorgan Chase" in first(after, "final")["answer"]


async def test_johnson_is_disambiguated_by_the_resolver(client: httpx.AsyncClient) -> None:
    thread, evs = await ask(client, "How risky is Johnson?")
    assert first(evs, "clarification_needed")["source"] == "entity_resolver"
    after = await resume(client, thread, "Johnson & Johnson (JNJ)")
    assert "talc" in first(after, "final")["answer"]


async def test_advice_is_refused_without_the_llm(client: httpx.AsyncClient) -> None:
    _, evs = await ask(client, "Should I buy Tesla?")
    llm_nodes = {"query_analyzer", "planner", "critic", "synthesizer"}
    assert not [e for e in evs if e["event"] == "node" and e["node"] in llm_nodes]
    assert "investment advice" in first(evs, "final")["answer"]


async def test_unknown_question_gets_a_plain_answer(client: httpx.AsyncClient) -> None:
    _, evs = await ask(client, "What is the weather in Paris?")
    assert kinds(evs)[-1] == "final"


def test_scripted_runtime_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="scripted"):
        Settings(_env_file=None, app_env="production", agent_runtime="scripted")
