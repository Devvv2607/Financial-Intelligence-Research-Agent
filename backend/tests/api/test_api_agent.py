"""The agent over HTTP: SSE runs, interrupt -> resume, thread history, guards.

The real FastAPI app with its real lifespan, routes and run manager; only the agent's
outside world is scripted (FakeLLM, fake tools, in-memory checkpointer and thread
store). Zero Groq calls.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from agent.checkpoint import memory_saver
from api.events import EVENT_LOG_TTL_S
from api.main import create_app
from api.runtime import AgentRuntime, make_runtime
from api.schemas import AGENT_EVENT
from core.config import Settings
from core.threads import MemoryThreadStore

from ..agent.fakes import OK_CRITIQUE, FakeLLM, FakeTools, analysis, ev, make_deps, ok, step

Q = "What are NVIDIA's main risks?"


def settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,  # a developer's .env must not leak into tests
        "app_env": "test",
        "groq_api_key": SecretStr("gsk_test"),
        "api_runs_per_minute": 100,
    }
    return Settings(**(base | kw))


def nvda_llm(synth: str = "NVIDIA faces export controls on China sales [1].") -> FakeLLM:
    return FakeLLM(
        {
            "query_analyzer": [analysis(Q, ["NVIDIA"])],
            "planner": [
                {
                    "steps": [
                        step(
                            "a",
                            "filings",
                            entities=["NVDA"],
                            tool_calls=[("get_risk_factors", '{"ticker": "NVDA"}')],
                        )
                    ]
                }
            ],
            "critic": [OK_CRITIQUE],
        },
        {"synthesizer": [synth]},
    )


def nvda_tools(delay: float = 0.0) -> FakeTools:
    return FakeTools(
        {"get_risk_factors": ok("get_risk_factors", {"risk_factors": [1]}, [ev("rf1")])},
        delay=delay,
    )


class Api:
    """App + client + handles on the fakes."""

    def __init__(self, client: httpx.AsyncClient, rt: AgentRuntime, recorded: list[Any]):
        self.client, self.rt, self.recorded = client, rt, recorded

    async def thread(self) -> str:
        r = await self.client.post("/api/threads", json={})
        assert r.status_code == 201
        return str(r.json()["id"])

    async def run(self, thread: str, query: str) -> httpx.Response:
        return await self.client.post(f"/api/threads/{thread}/runs", json={"query": query})


@asynccontextmanager
async def api(llm: FakeLLM, tools: FakeTools, **kw: Any) -> AsyncIterator[Api]:
    recorded: list[dict[str, Any]] = []
    holder: dict[str, AgentRuntime] = {}

    @asynccontextmanager
    async def factory(*_: Any) -> AsyncIterator[AgentRuntime]:
        rt = make_runtime(
            make_deps(llm, tools, recorded), memory_saver(), MemoryThreadStore(recorded)
        )
        holder["rt"] = rt
        try:
            yield rt
        finally:
            await rt.runs.cancel_all()

    app = create_app(settings(**kw), runtime_factory=factory)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client,
    ):
        yield Api(client, holder["rt"], recorded)


def events(resp: httpx.Response) -> list[dict[str, Any]]:
    """Parse an SSE body; every event must validate against the typed union."""
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    out, last_id = [], 0
    for block in resp.text.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if line and not line.startswith(":")
        )
        if not fields:
            continue
        data = json.loads(fields["data"])
        assert fields["event"] == data["event"]
        assert int(fields["id"]) == last_id + 1  # ids are a gapless sequence
        last_id += 1
        AGENT_EVENT.validate_python(data)
        out.append(data)
    return out


def first(evs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(e for e in evs if e["event"] == kind)


def kinds(evs: list[dict[str, Any]]) -> list[str]:
    """Domain event types in order (node events left out), consecutive duplicates
    collapsed (tokens)."""
    out: list[str] = []
    for e in evs:
        if e["event"] != "node" and (not out or out[-1] != e["event"]):
            out.append(e["event"])
    return out


# ---------------------------------------------------------------- full run
async def test_full_run_streams_typed_events_and_history() -> None:
    async with api(nvda_llm(), nvda_tools()) as a:
        thread = await a.thread()
        evs = events(await a.run(thread, Q))

        assert kinds(evs) == [
            "run_started",
            "plan_created",
            "step_started",
            "tool_called",
            "evidence_added",
            "step_completed",
            "critique",
            "token",
            "final",
        ]
        started = evs[0]
        assert started["thread_id"] == thread and not started["resumed"]
        assert first(evs, "plan_created")["steps"][0]["id"] == "r1s1"
        assert first(evs, "evidence_added")["sources"][0]["source_id"] == "rf1"  # full cards
        done = next(e for e in evs if e["event"] == "step_completed")
        assert done["status"] == "done" and done["mode"] == "direct"
        assert done["duration_ms"] is not None
        tokens = "".join(e["text"] for e in evs if e["event"] == "token")
        assert tokens == "NVIDIA faces export controls on China sales [1]."  # streamed live
        final = first(evs, "final")
        assert final["answer"].startswith(tokens)  # + the footer added by finalize
        assert [c["source_id"] for c in final["citations"]] == ["rf1"]

        detail = (await a.client.get(f"/api/threads/{thread}")).json()
        assert detail["thread"]["title"] == Q  # first question names the thread
        assert detail["thread"]["status"] == "idle"
        assert detail["thread"]["last_run_id"] == started["run_id"]
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        assert detail["pending_clarification"] is None
        assert detail["latest"]["plan"][0]["id"] == "r1s1"
        assert detail["latest"]["citations"][0]["source_id"] == "rf1"
        assert detail["run_ids"] == [started["run_id"]]  # recorded once
        listed = (await a.client.get("/api/threads")).json()
        assert [t["id"] for t in listed] == [thread]


# ---------------------------------------------------------------- interrupt -> resume
async def test_clarification_interrupts_then_resume_finishes_over_http() -> None:
    q = "How risky is Johnson?"
    plan = {
        "steps": [
            step(
                "a",
                "filings",
                entities=["JNJ"],
                tool_calls=[("get_risk_factors", '{"ticker": "JNJ"}')],
            )
        ]
    }
    llm = FakeLLM(
        {
            "query_analyzer": [analysis(q, ["Johnson & Johnson"])],
            "planner": [plan],
            "critic": [OK_CRITIQUE],
        },
        {"synthesizer": ["JNJ risks [1]."]},
    )
    tools = FakeTools({"get_risk_factors": ok("get_risk_factors", {"x": [1]}, [ev("j", "JNJ")])})
    async with api(llm, tools) as a:
        thread = await a.thread()
        paused = events(await a.run(thread, q))
        assert kinds(paused) == ["run_started", "clarification_needed"]  # stream ends at the pause
        asked = first(paused, "clarification_needed")
        assert asked["source"] == "entity_resolver"
        assert any("JNJ" in o for o in asked["options"])

        detail = (await a.client.get(f"/api/threads/{thread}")).json()
        assert detail["thread"]["status"] == "awaiting_clarification"
        assert detail["pending_clarification"]["question"] == asked["question"]
        # A new question can't jump the pending one.
        blocked = await a.run(thread, "Something else")
        assert blocked.status_code == 409

        resumed = events(
            await a.client.post(
                f"/api/threads/{thread}/resume", json={"answer": "Johnson & Johnson (JNJ)"}
            )
        )
        assert resumed[0]["event"] == "run_started" and resumed[0]["resumed"]
        assert resumed[0]["run_id"] == paused[0]["run_id"]  # one run, paused and continued
        # The question is not repeated on resume (LangGraph re-runs the interrupted node).
        assert "clarification_needed" not in kinds(resumed)
        assert kinds(resumed)[-1] == "final"
        assert first(resumed, "final")["citations"][0]["source_id"] == "j"

        detail = (await a.client.get(f"/api/threads/{thread}")).json()
        assert detail["thread"]["status"] == "idle"
        assert detail["pending_clarification"] is None
        assert [e["ticker"] for e in detail["latest"]["evidence"]] == ["JNJ"]
        assert len(a.recorded) == 1  # recorded once, at the end


# ---------------------------------------------------------------- guards
async def test_unknown_thread_bad_input_and_nothing_to_resume() -> None:
    async with api(nvda_llm(), nvda_tools(), api_max_question_chars=50) as a:
        missing = uuid.uuid4()
        assert (await a.client.get(f"/api/threads/{missing}")).status_code == 404
        assert (await a.run(str(missing), Q)).status_code == 404
        thread = await a.thread()
        assert (await a.run(thread, "x" * 51)).status_code == 422
        assert (await a.run(thread, "   ")).status_code == 422
        r = await a.client.post(f"/api/threads/{thread}/resume", json={"answer": "JPM"})
        assert r.status_code == 409


async def test_second_run_on_a_busy_thread_is_refused() -> None:
    async with api(nvda_llm(), nvda_tools(delay=0.5)) as a:
        thread = await a.thread()
        first = asyncio.create_task(a.run(thread, Q))
        await _until(lambda: a.rt.runs.is_active(uuid.UUID(thread)))
        assert (await a.run(thread, Q)).status_code == 409
        assert kinds(events(await first))[-1] == "final"


async def test_runs_are_rate_limited_per_client() -> None:
    async with api(nvda_llm(), nvda_tools(), api_runs_per_minute=2) as a:
        statuses = []
        for _ in range(3):
            thread = await a.thread()
            r = await a.run(thread, Q)
            statuses.append(r.status_code)
        assert statuses == [200, 200, 429]
        assert int(r.headers["Retry-After"]) >= 1


async def test_run_continues_and_is_recorded_without_a_listener() -> None:
    """A closed tab doesn't kill the run: it is driven by a task, not by the response."""
    async with api(nvda_llm(), nvda_tools(delay=0.05)) as a:
        thread = uuid.UUID(await a.thread())
        a.rt.runs.start(
            thread, {"messages": [HumanMessage(content=Q)]}, str(uuid.uuid4()), resumed=False
        )
        await a.rt.runs.wait_idle()  # nobody reads the stream
        assert len(a.recorded) == 1
        detail = (await a.client.get(f"/api/threads/{thread}")).json()
        assert detail["thread"]["status"] == "idle"
        assert detail["messages"][-1]["role"] == "assistant"


async def test_unexpected_failure_ends_the_stream_with_an_error_event() -> None:
    def boom(_: Any) -> Any:
        raise RuntimeError("planner exploded")

    llm = FakeLLM({"query_analyzer": [analysis(Q, ["NVIDIA"])], "planner": boom})
    async with api(llm, nvda_tools()) as a:
        thread = await a.thread()
        evs = events(await a.run(thread, Q))
        err = first(evs, "error")
        assert kinds(evs)[-1] == "error" and err["code"] == "internal"
        assert "planner exploded" not in err["message"]  # no internals leak to clients
        detail = (await a.client.get(f"/api/threads/{thread}")).json()
        assert detail["thread"]["status"] == "error"


# ---------------------------------------------------------------- graph
async def test_graph_endpoint_exposes_nodes_edges_and_subgraphs() -> None:
    async with api(nvda_llm(), nvda_tools()) as a:
        g = (await a.client.get("/api/graph")).json()
    ids = {n["id"] for n in g["nodes"]}
    assert {"start_turn", "guardrail", "planner", "dispatcher", "critic", "finalize"} <= ids
    sub = {n["label"] for n in g["nodes"] if n["subgraph"] == "filings_agent"}
    assert {"prepare", "direct", "llm", "tools", "finish"} <= sub
    edges = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("synthesizer", "finalize") in edges
    assert any(e["conditional"] for e in g["edges"])


@pytest.mark.parametrize("path", ["/api/threads", "/api/graph"])
async def test_agent_endpoints_503_without_a_runtime(path: str) -> None:
    app = create_app(settings(), runtime_factory=None)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        assert (await client.get(path)).status_code == 503


async def _until(cond: Callable[[], bool], limit_s: float = 5.0) -> None:
    async with asyncio.timeout(limit_s):
        # polls a plain flag owned by the app under test; there is no event to wait on
        while not cond():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


# ---------------------------------------------------------------- node events
async def test_node_events_trace_the_graph_with_api_graph_ids() -> None:
    async with api(nvda_llm(), nvda_tools()) as a:
        graph_ids = {n["id"] for n in (await a.client.get("/api/graph")).json()["nodes"]}
        evs = events(await a.run(await a.thread(), Q))
    nodes = [e for e in evs if e["event"] == "node"]
    starts = [e["node"] for e in nodes if e["phase"] == "start"]
    assert starts == [
        "start_turn",
        "guardrail",
        "entity_resolver",
        "query_analyzer",
        "planner",
        "dispatcher",
        "filings_agent",
        "filings_agent:prepare",
        "filings_agent:direct",
        "filings_agent:finish",
        "join",
        "dispatcher",
        "critic",
        "synthesizer",
        "finalize",
    ]
    # Every node is one /api/graph drew; a specialist's own start/end ("filings_agent")
    # maps to its subgraph group (xray lists only the subgraph's inner nodes).
    groups = {n.split(":")[0] for n in graph_ids if ":" in n}
    assert set(starts) <= graph_ids | groups
    ends = [e["node"] for e in nodes if e["phase"] == "end"]
    assert sorted(ends) == sorted(starts)
    inner = [e for e in nodes if e["node"].startswith("filings_agent")]
    assert {e["step_id"] for e in inner} == {"r1s1"}
    assert not any(e["error"] for e in nodes)
    # node events interleave with domain events in execution order
    order = [e["event"] if e["event"] != "node" else e["node"] for e in evs]
    assert order.index("planner") < order.index("plan_created") < order.index("dispatcher")


async def test_parallel_specialists_are_told_apart_by_step_id() -> None:
    plan = {
        "steps": [
            step(
                "a",
                "filings",
                entities=["NVDA"],
                tool_calls=[("get_risk_factors", '{"ticker": "NVDA"}')],
            ),
            step(
                "b",
                "filings",
                entities=["AMD"],
                tool_calls=[("get_risk_factors", '{"ticker": "AMD"}')],
            ),
        ]
    }
    llm = FakeLLM(
        {
            "query_analyzer": [analysis(Q, ["NVIDIA", "AMD"])],
            "planner": [plan],
            "critic": [OK_CRITIQUE],
        },
        {"synthesizer": ["Both [1][2]."]},
    )
    async with api(llm, nvda_tools(delay=0.05)) as a:
        evs = events(await a.run(await a.thread(), Q))
    direct = [e for e in evs if e["event"] == "node" and e["node"] == "filings_agent:direct"]
    assert {e["step_id"] for e in direct} == {"r1s1", "r1s2"}
    assert len(direct) == 4  # start + end for each parallel step


async def test_clarification_node_ends_interrupted() -> None:
    llm = FakeLLM({"query_analyzer": [analysis("How risky is Johnson?", [])]})
    async with api(llm, nvda_tools()) as a:
        evs = events(await a.run(await a.thread(), "How risky is Johnson?"))
    ask = [e for e in evs if e["event"] == "node" and e["node"] == "ask_clarification"]
    assert [e["phase"] for e in ask] == ["start", "end"]
    assert ask[-1]["interrupted"] is True


# ---------------------------------------------------------------- replayable event log
async def test_reconnect_replays_missed_events_then_ends_with_the_run() -> None:
    async with api(nvda_llm(), nvda_tools(delay=0.2)) as a:
        thread = await a.thread()
        first = asyncio.create_task(a.run(thread, Q))
        await _until(lambda: a.rt.runs.log_for(uuid.UUID(thread)) is not None)
        log = a.rt.runs.log_for(uuid.UUID(thread))
        assert log is not None
        await _until(lambda: len(log.events) >= 5 and not log.done)
        # A client that saw events 1-3, then lost the connection mid-run:
        resumed = await a.client.get(f"/api/threads/{thread}/events", params={"after": 3})
        via_header = await a.client.get(
            f"/api/threads/{thread}/events", headers={"Last-Event-ID": "3"}
        )
        full = _raw_events(await first)
        tail, tail2 = _raw_events(resumed), _raw_events(via_header)
        assert tail == full[3:] == tail2  # same events, same ids, nothing missed
        assert tail[0][0] == 4 and kinds([e for _, e in tail])[-1] == "final"
        # After the run: a reload replays the whole run from the log.
        replay = _raw_events(await a.client.get(f"/api/threads/{thread}/events"))
        assert replay == full


async def test_event_log_404s_when_gone_or_for_another_run() -> None:
    async with api(nvda_llm(), nvda_tools()) as a:
        thread = await a.thread()
        never_ran = await a.client.get(f"/api/threads/{thread}/events")
        assert never_ran.status_code == 404
        run_id = events(await a.run(thread, Q))[0]["run_id"]
        url = f"/api/threads/{thread}/events"
        assert (await a.client.get(url, params={"run_id": run_id})).status_code == 200
        other = await a.client.get(url, params={"run_id": str(uuid.uuid4())})
        assert other.status_code == 404
        # TTL: the log is dropped EVENT_LOG_TTL_S after the run ended (then: poll the thread)
        now = time.monotonic()
        a.rt.runs.clock = lambda: now + EVENT_LOG_TTL_S + 1
        assert (await a.client.get(url)).status_code == 404
        assert (await a.client.get(f"/api/threads/{thread}")).json()["latest"]["citations"]


def _raw_events(resp: httpx.Response) -> list[tuple[int, dict[str, Any]]]:
    out = []
    for block in resp.text.split("\n\n"):
        f = dict(x.split(": ", 1) for x in block.splitlines() if x and not x.startswith(":"))
        if f:
            out.append((int(f["id"]), json.loads(f["data"])))
    return out
