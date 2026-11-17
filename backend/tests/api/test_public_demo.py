"""Public demo mode over HTTP: per-visitor threads, daily cap, the one-run gate, replays.

Same harness as test_api_agent (real app, scripted LLM and tools, in-memory stores).
Zero Groq calls.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from api.main import create_app
from api.ratelimit import DailyCap
from core.config import Settings

from .test_api_agent import Q, api, events, kinds, nvda_llm, nvda_tools


def visitor() -> dict[str, str]:
    return {"X-Visitor-Id": str(uuid.uuid4())}


async def test_threads_are_private_to_their_visitor() -> None:
    async with api(nvda_llm(), nvda_tools()) as a:
        mine = await a.thread()
        assert kinds(events(await a.run(mine, Q)))[-1] == "final"
        other = visitor()
        c = a.client

        assert [t["id"] for t in (await c.get("/api/threads")).json()] == [mine]
        assert (await c.get("/api/threads", headers=other)).json() == []
        # Every per-thread route answers 404 to another visitor, as for an unknown id.
        assert (await c.get(f"/api/threads/{mine}", headers=other)).status_code == 404
        r = await c.post(f"/api/threads/{mine}/runs", json={"query": Q}, headers=other)
        assert r.status_code == 404
        r = await c.post(f"/api/threads/{mine}/resume", json={"answer": "x"}, headers=other)
        assert r.status_code == 404
        assert (await c.get(f"/api/threads/{mine}/events", headers=other)).status_code == 404
        assert (await c.get(f"/api/threads/{mine}")).status_code == 200


async def test_threads_without_an_owner_are_hidden_from_everyone() -> None:
    async with api(nvda_llm(), nvda_tools()) as a:
        old = await a.rt.threads.ensure(uuid.uuid4(), "an old local test run")  # no visitor
        assert (await a.client.get("/api/threads")).json() == []
        assert (await a.client.get(f"/api/threads/{old.id}")).status_code == 404


async def test_a_missing_or_malformed_visitor_id_is_refused() -> None:
    async with api(nvda_llm(), nvda_tools()) as a:
        for headers in ({"X-Visitor-Id": ""}, {"X-Visitor-Id": "../etc"}):
            r = await a.client.get("/api/threads", headers=headers)
            assert r.status_code == 400 and "X-Visitor-Id" in r.json()["detail"]
        a.client.headers.pop("X-Visitor-Id")
        assert (await a.client.post("/api/threads", json={})).status_code == 400


# ---------------------------------------------------------------- daily live-run cap
TESLA = "Should I buy Tesla?"  # the scripted runtime answers it at once, no LLM


@asynccontextmanager
async def scripted_app(**kw: Any) -> AsyncIterator[httpx.AsyncClient]:
    base: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "agent_runtime": "scripted",
        "groq_api_key": SecretStr("gsk_unused"),
        "api_runs_per_minute": 100,
        "replays_dir": Path("does-not-exist"),
    }
    app = create_app(Settings(**(base | kw)))
    transport = httpx.ASGITransport(app=app, client=("198.51.100.20", 1234))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c,
    ):
        yield c


async def live_run(
    c: httpx.AsyncClient, who: dict[str, str], q: str = TESLA, **headers: str
) -> httpx.Response:
    thread = (await c.post("/api/threads", json={}, headers=who)).json()["id"]
    return await c.post(f"/api/threads/{thread}/runs", json={"query": q}, headers=who | headers)


async def test_each_visitor_gets_the_daily_cap_then_a_friendly_429() -> None:
    async with scripted_app(live_runs_per_visitor_per_day=2, live_runs_per_ip_per_day=10) as c:
        me, other = visitor(), visitor()
        assert [(await live_run(c, me)).status_code for _ in range(3)] == [200, 200, 429]
        r = await live_run(c, me)
        assert r.status_code == 429 and r.headers["X-Limit-Reason"] == "daily-cap"
        assert "live questions" in r.json()["detail"] and "recorded runs" in r.json()["detail"]
        assert (await live_run(c, other)).status_code == 200  # someone else's count


async def test_a_new_visitor_id_does_not_reset_the_ip_cap() -> None:
    async with scripted_app(live_runs_per_visitor_per_day=5, live_runs_per_ip_per_day=2) as c:
        codes = [(await live_run(c, visitor())).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


async def test_the_ip_header_is_used_only_when_configured() -> None:
    caps = {"live_runs_per_visitor_per_day": 5, "live_runs_per_ip_per_day": 1}
    async with scripted_app(**caps) as c:  # no CLIENT_IP_HEADER: header ignored
        assert (await live_run(c, visitor(), **{"X-Real-IP": "203.0.113.1"})).status_code == 200
        r = await live_run(c, visitor(), **{"X-Real-IP": "203.0.113.2"})
        assert r.status_code == 429  # same socket address, so the same IP key
    async with scripted_app(client_ip_header="X-Real-IP", **caps) as c:  # behind the proxy
        assert (await live_run(c, visitor(), **{"X-Real-IP": "203.0.113.1"})).status_code == 200
        assert (await live_run(c, visitor(), **{"X-Real-IP": "203.0.113.2"})).status_code == 200
        r = await live_run(c, visitor(), **{"X-Real-IP": "203.0.113.1"})
        assert r.status_code == 429


async def test_the_cap_is_checked_before_question_validation() -> None:
    """Zero-token production check: once capped, an over-long question still gets the
    cap's 429 (not 422), so a spoofed IP header can be tested without starting a run."""
    async with scripted_app(live_runs_per_visitor_per_day=1, live_runs_per_ip_per_day=1) as c:
        me = visitor()
        assert (await live_run(c, me)).status_code == 200
        assert (await live_run(c, visitor(), q="x" * 5000)).status_code == 429


async def test_a_resume_finishes_the_question_without_counting() -> None:
    async with scripted_app(live_runs_per_visitor_per_day=1, live_runs_per_ip_per_day=5) as c:
        me = visitor()
        thread = (await c.post("/api/threads", json={}, headers=me)).json()["id"]
        r = await c.post(
            f"/api/threads/{thread}/runs", json={"query": "How risky is the bank?"}, headers=me
        )
        assert "clarification_needed" in kinds(events(r))
        r = await c.post(
            f"/api/threads/{thread}/resume", json={"answer": "JPMorgan Chase (JPM)"}, headers=me
        )
        assert "final" in kinds(events(r))
        assert (await live_run(c, me)).status_code == 429  # the next question is capped


def test_the_cap_resets_at_the_next_utc_day() -> None:
    day = [date(2026, 11, 17)]
    cap = DailyCap(per_visitor=1, per_ip=1, today=lambda: day[0])
    assert cap.take("v", "ip") and not cap.take("v", "ip")
    day[0] = date(2026, 11, 18)
    assert cap.take("v", "ip")


# ---------------------------------------------------------------- one live run at a time
COMPARE = "Compare the top risk factors NVIDIA and AMD disclosed in their latest 10-Ks."


async def test_a_second_run_waits_in_the_queue_and_then_runs() -> None:
    async with scripted_app(live_runs_per_visitor_per_day=10, live_runs_per_ip_per_day=10) as c:
        first = asyncio.create_task(live_run(c, visitor(), COMPARE))
        await asyncio.sleep(0.2)  # the scripted comparison holds the slot for seconds
        second = events(await live_run(c, visitor()))
        assert kinds(second)[:2] == ["run_started", "queued"]
        assert second[1]["position"] == 1
        assert kinds(second)[-1] in {"final", "node"} and "final" in kinds(second)
        assert "final" in kinds(events(await first))


async def test_a_full_queue_refuses_the_run_before_it_starts() -> None:
    caps = {"live_runs_per_visitor_per_day": 10, "live_runs_per_ip_per_day": 10}
    async with scripted_app(live_queue_max=0, **caps) as c:
        me = visitor()
        first = asyncio.create_task(live_run(c, visitor(), COMPARE))
        await asyncio.sleep(0.2)
        r = await live_run(c, me)
        assert r.status_code == 503 and r.headers["X-Limit-Reason"] == "queue-full"
        assert "one live question at a time" in r.json()["detail"]
        await first
        assert (await live_run(c, me)).status_code == 200  # the refusal didn't count
