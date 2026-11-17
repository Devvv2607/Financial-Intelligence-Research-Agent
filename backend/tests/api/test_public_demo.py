"""Public demo mode over HTTP: per-visitor threads, daily cap, the one-run gate, replays.

Same harness as test_api_agent (real app, scripted LLM and tools, in-memory stores).
Zero Groq calls.
"""

import uuid

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
