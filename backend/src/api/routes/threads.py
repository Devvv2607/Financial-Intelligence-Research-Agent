"""Threads: create/list, history from the checkpointer, runs and resumes streamed as SSE."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.sse import KEEPALIVE_COMMENT, EventSourceResponse, format_sse_event
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from api.ratelimit import limit_runs
from api.runtime import AgentRuntime, EventQueue, RunConflictError, thread_config
from api.schemas import (
    CreateThread,
    LatestTurn,
    Message,
    PendingClarification,
    ResumeRequest,
    RunRequest,
    Thread,
    ThreadDetail,
)
from api.threads import title_from
from core.config import Settings

router = APIRouter(prefix="/api/threads", tags=["threads"])
KEEPALIVE_S = 15.0  # proxies drop idle streams; a comment line keeps them open
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
# Returned as a response object rather than via FastAPI's generator-style SSE routes, so
# 404/409/422/429 can be raised before the stream starts (D-041).
SSE_DOC: dict[int | str, dict[str, Any]] = {
    200: {"description": "SSE stream of typed agent events", "content": {"text/event-stream": {}}}
}


def runtime_of(request: Request) -> AgentRuntime:
    rt: AgentRuntime | None = request.app.state.runtime
    if rt is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The agent is not available.")
    return rt


async def _thread_or_404(rt: AgentRuntime, thread_id: uuid.UUID) -> Thread:
    thread = await rt.threads.get(thread_id)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found.")
    return thread


async def sse(queue: EventQueue) -> AsyncIterator[bytes]:
    seq = 0
    while True:
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
        except TimeoutError:
            yield KEEPALIVE_COMMENT
            continue
        if ev is None:
            return
        seq += 1
        yield format_sse_event(event=ev.event, data_str=ev.model_dump_json(), id=str(seq))


def _stream(queue: EventQueue) -> EventSourceResponse:
    return EventSourceResponse(sse(queue), headers=SSE_HEADERS)


@router.post("", response_model=Thread, status_code=status.HTTP_201_CREATED)
async def create_thread(body: CreateThread, request: Request) -> Thread:
    return await runtime_of(request).threads.create(body.title)


@router.get("", response_model=list[Thread])
async def list_threads(request: Request, limit: int = 50) -> list[Thread]:
    return await runtime_of(request).threads.recent(min(max(limit, 1), 200))


@router.get("/{thread_id}", response_model=ThreadDetail)
async def get_thread(thread_id: uuid.UUID, request: Request) -> ThreadDetail:
    rt = runtime_of(request)
    thread = await _thread_or_404(rt, thread_id)
    snap = await rt.graph.aget_state(thread_config(thread_id))
    values: dict[str, Any] = snap.values or {}
    messages = [
        Message(role="user" if isinstance(m, HumanMessage) else "assistant", content=str(m.content))
        for m in values.get("messages", [])
        if isinstance(m, HumanMessage | AIMessage)
    ]
    pending = None
    if snap.interrupts:
        v = snap.interrupts[0].value
        pending = PendingClarification(
            question=v.get("question", ""), options=v.get("options", []), source=v.get("source")
        )
    latest = None
    if values.get("query"):
        latest = LatestTurn(
            run_id=values.get("run_id"),
            query=values.get("query"),
            route=values.get("route"),
            plan=values.get("plan", []),
            step_results=values.get("step_results", {}),
            evidence=values.get("evidence", []),
            citations=values.get("citations", []),
            citation_check=values.get("citation_check"),
        )
    return ThreadDetail(
        thread=thread,
        messages=messages,
        pending_clarification=pending,
        latest=latest,
        run_ids=await rt.threads.run_ids(thread_id),
    )


@router.post("/{thread_id}/runs", responses=SSE_DOC, dependencies=[Depends(limit_runs)])
async def start_run(
    thread_id: uuid.UUID, body: RunRequest, request: Request
) -> EventSourceResponse:
    """Start a research run; the response is the SSE stream of its typed events."""
    rt = runtime_of(request)
    settings: Settings = request.app.state.settings
    query = body.query.strip()
    if not query or len(query) > settings.api_max_question_chars:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"The question must be 1-{settings.api_max_question_chars} characters.",
        )
    await _thread_or_404(rt, thread_id)
    if rt.runs.is_active(thread_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A run is already in progress on this thread."
        )
    snap = await rt.graph.aget_state(thread_config(thread_id))
    if snap.interrupts:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This thread is waiting for a clarification; answer it with /resume first.",
        )
    await rt.threads.set_title_if_empty(thread_id, title_from(query))
    try:
        queue = rt.runs.start(
            thread_id,
            {"messages": [HumanMessage(content=query)]},
            str(uuid.uuid4()),
            resumed=False,
        )
    except RunConflictError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A run is already in progress on this thread."
        ) from None
    return _stream(queue)


@router.post("/{thread_id}/resume", responses=SSE_DOC, dependencies=[Depends(limit_runs)])
async def resume_run(
    thread_id: uuid.UUID, body: ResumeRequest, request: Request
) -> EventSourceResponse:
    """Answer the pending clarification (`Command(resume=...)`); streams the rest of the run."""
    rt = runtime_of(request)
    await _thread_or_404(rt, thread_id)
    if rt.runs.is_active(thread_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A run is already in progress on this thread."
        )
    snap = await rt.graph.aget_state(thread_config(thread_id))
    if not snap.interrupts:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing to resume on this thread.")
    # Same run id as the paused question: one run, recorded once.
    run_id = str(snap.values.get("run_id") or uuid.uuid4())
    try:
        queue = rt.runs.start(thread_id, Command(resume=body.answer.strip()), run_id, resumed=True)
    except RunConflictError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A run is already in progress on this thread."
        ) from None
    return _stream(queue)
