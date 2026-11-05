"""The agent inside the API: one compiled graph per process, runs in background tasks.

A run is driven by a background task, not by the HTTP response: it appends typed events
to the run's log (api/events.py), which any number of SSE responses read and follow. If
the client disconnects (closed tab, dropped network), the run still finishes and is
recorded, and the client can replay the missed events or read the thread back, so the
tokens it spent aren't wasted. One active run per
thread; a second one is refused (409) instead of racing on the same checkpoint.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import weaviate
from langchain_core.messages import AIMessageChunk
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.checkpoint import postgres_saver
from agent.deps import AgentDeps
from agent.graph import RECURSION_LIMIT, build_graph
from agent.llm import GroqAgentLLM
from agent.runlog import make_run_recorder
from agent.tools import call_tool
from agent.tools.base import ToolContext
from agent.tools.common import Company, fiscal_periods, resolve_tickers
from api.events import NodeTracker, RunLog
from api.schemas import AGENT_EVENT
from core.config import Settings
from core.db.llm_store import PostgresLLMStore
from core.embeddings import Embedder
from core.llm import GroqClient
from core.newsapi import NewsApiClient
from core.threads import PostgresThreadStore, ThreadStatus, ThreadStore

logger = logging.getLogger(__name__)


class RunConflictError(Exception):
    """A run is already in progress on this thread."""


def thread_config(thread_id: uuid.UUID, run_id: str | None = None) -> RunnableConfig:
    cfg: dict[str, Any] = {"thread_id": str(thread_id)}
    if run_id is not None:
        # Not `run_id`: LangGraph reserves that key (D-033).
        cfg["agent_run_id"] = run_id
    return {"configurable": cfg, "recursion_limit": RECURSION_LIMIT}


@dataclass
class RunManager:
    graph: Any  # CompiledStateGraph
    threads: ThreadStore
    clock: Callable[[], float] = time.monotonic
    _active: dict[uuid.UUID, asyncio.Task[None]] = field(default_factory=dict)
    _logs: dict[uuid.UUID, RunLog] = field(default_factory=dict)

    def is_active(self, thread_id: uuid.UUID) -> bool:
        return thread_id in self._active

    def start(self, thread_id: uuid.UUID, payload: Any, run_id: str, *, resumed: bool) -> RunLog:
        # Check-and-set without an await in between: atomic on the event loop.
        if thread_id in self._active:
            raise RunConflictError(str(thread_id))
        self._expire()
        log = RunLog(run_id)
        self._logs[thread_id] = log  # a thread's newest run replaces its previous log
        task = asyncio.create_task(self._drive(thread_id, payload, run_id, resumed, log))
        self._active[thread_id] = task
        task.add_done_callback(lambda _: self._active.pop(thread_id, None))
        return log

    def log_for(self, thread_id: uuid.UUID) -> RunLog | None:
        """The thread's current or recently finished run log (None after TTL / restart)."""
        self._expire()
        return self._logs.get(thread_id)

    def _expire(self) -> None:
        now = self.clock()
        for tid in [t for t, log in self._logs.items() if log.expired(now)]:
            del self._logs[tid]

    async def wait_idle(self) -> None:
        """Wait for every running task (tests; graceful shutdown)."""
        while self._active:
            await asyncio.gather(*self._active.values(), return_exceptions=True)

    async def cancel_all(self) -> None:
        for t in list(self._active.values()):
            t.cancel()
        await asyncio.gather(*self._active.values(), return_exceptions=True)

    async def _drive(
        self,
        thread_id: uuid.UUID,
        payload: Any,
        run_id: str,
        resumed: bool,
        log: RunLog,
    ) -> None:
        config = thread_config(thread_id, run_id)
        status: ThreadStatus = "idle"
        nodes = NodeTracker()
        await self._safe_status(thread_id, "running", uuid.UUID(run_id))
        self._put(
            log,
            {
                "event": "run_started",
                "run_id": run_id,
                "thread_id": str(thread_id),
                "resumed": resumed,
            },
        )
        try:
            async for ns, mode, chunk in self.graph.astream(
                payload, config, stream_mode=["custom", "messages", "tasks"], subgraphs=True
            ):
                if mode == "custom" and isinstance(chunk, dict):
                    self._put(log, chunk)
                elif mode == "tasks":
                    self._put(log, nodes.event(ns, chunk))
                elif mode == "messages":
                    msg, meta = chunk
                    if (
                        meta.get("langgraph_node") == "synthesizer"
                        and isinstance(msg, AIMessageChunk)
                        and msg.content
                    ):
                        self._put(log, {"event": "token", "text": str(msg.content)})
            snap = await self.graph.aget_state(config)
            if snap.interrupts:
                status = "awaiting_clarification"
        except asyncio.CancelledError:
            status = "error"
            raise
        except Exception as exc:  # the stream must end with an event, never just stop
            logger.exception("run failed", extra={"run_id": run_id, "thread_id": str(thread_id)})
            status = "error"
            self._put(
                log,
                {
                    "event": "error",
                    "code": "internal",
                    "message": f"The run failed ({type(exc).__name__}). Try again.",
                },
            )
        finally:
            await self._safe_status(thread_id, status)
            log.close(self.clock)

    @staticmethod
    def _put(log: RunLog, raw: dict[str, Any]) -> None:
        try:
            log.append(AGENT_EVENT.validate_python(raw))
        except ValidationError:
            # An event shape the API doesn't know is a bug in the graph: log it, keep going.
            logger.error("dropped invalid agent event", extra={"event": raw.get("event")})

    async def _safe_status(
        self, thread_id: uuid.UUID, status: ThreadStatus, run_id: uuid.UUID | None = None
    ) -> None:
        try:
            await self.threads.set_status(thread_id, status, run_id)
        except Exception:  # bookkeeping must not break a run
            logger.exception("thread status update failed", extra={"thread_id": str(thread_id)})


@dataclass
class AgentRuntime:
    graph: Any
    threads: ThreadStore
    runs: RunManager


# (settings, engine, weaviate client) -> runtime; tests pass one with fakes.
RuntimeFactory = Callable[[Settings, AsyncEngine, Any], AbstractAsyncContextManager[AgentRuntime]]


@asynccontextmanager
async def live_runtime(
    settings: Settings, engine: AsyncEngine, wv: weaviate.WeaviateAsyncClient
) -> AsyncIterator[AgentRuntime]:
    """The real agent: Groq (agent model), tools on the app's DB/Weaviate, Postgres saver."""
    async with AsyncExitStack() as stack:
        news = (
            NewsApiClient(
                settings.newsapi_key.get_secret_value(), settings.newsapi_base_url, settings.raw_dir
            )
            if settings.newsapi_key and settings.news_live_fallback_enabled
            else None
        )
        if news is not None:
            stack.push_async_callback(news.aclose)
        tool_llm = GroqClient(settings, PostgresLLMStore(engine), agent=True)
        stack.push_async_callback(tool_llm.aclose)
        embedder = Embedder(settings.embedding_model, settings.models_dir)
        ctx = ToolContext(
            settings=settings,
            engine=engine,
            weaviate=wv,
            embedder=embedder,
            llm=tool_llm,
            news=news,
        )
        llm = GroqAgentLLM(settings, PostgresLLMStore(engine))
        stack.push_async_callback(llm.aclose)

        async def companies() -> list[Company]:
            return (await resolve_tickers(ctx, None))[0]

        deps = AgentDeps(
            llm=llm,
            call_tool=partial(call_tool, ctx),
            list_companies=companies,
            record_run=make_run_recorder(engine, settings),
            fiscal_periods=partial(fiscal_periods, ctx),
            embed=embedder.embed,
        )
        saver = await stack.enter_async_context(postgres_saver(settings.database_url))
        runtime = make_runtime(deps, saver, PostgresThreadStore(engine))
        try:
            yield runtime
        finally:
            await runtime.runs.cancel_all()


def make_runtime(
    deps: AgentDeps, saver: BaseCheckpointSaver[Any], threads: ThreadStore
) -> AgentRuntime:
    graph = build_graph(deps, saver)
    return AgentRuntime(graph=graph, threads=threads, runs=RunManager(graph, threads))
