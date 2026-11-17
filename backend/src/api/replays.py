"""Recorded runs: real SSE event streams saved as JSON fixtures, played back at zero tokens.

A replay is what a past run really streamed, event for event, with each event's arrival
time (`t_ms`). The public demo's example chips play these, labeled as recordings in the
UI ("Recorded live run · <date> · not live"); a replay is never presented as a live run.
Fixtures are written by `python -m api.replay_recorder` (which refuses anything that
looks like a key or an IP address) and live in `REPLAYS_DIR` (default `replays/`).

A run that paused for a clarification has two segments: the question, then the resume
with the answer the recorder gave. Each segment is streamed separately, with SSE ids
from 1, exactly as the live run and resume endpoints stream them.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.sse import EventSourceResponse, format_sse_event
from pydantic import BaseModel, Field, ValidationError

from api.schemas import AgentEvent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/replays", tags=["replays"])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Compressed timing: a replay should feel like the run but take seconds, not a minute.
SPEEDUP = 4.0
MAX_GAP_MS = 600  # a long tool call or LLM wait becomes at most this pause
MAX_TOKEN_GAP_MS = 12  # streamed answer tokens


class RecordedEvent(BaseModel):
    t_ms: int = Field(ge=0)  # arrival time since the segment's request was sent
    event: AgentEvent


class SegmentInput(BaseModel):
    kind: Literal["run", "resume"]
    text: str  # the question, or the clarification answer


class Segment(BaseModel):
    input: SegmentInput
    events: list[RecordedEvent]


class Replay(BaseModel):
    slug: str
    question: str
    recorded_at: datetime
    # "live": recorded from a real run (Groq + real data). "scripted": the zero-token
    # test runtime; only test fixtures may say so, and the UI labels them differently.
    source: Literal["live", "scripted"]
    api_url: str | None = None  # where it was recorded (a public URL, never an IP)
    tokens: int | None = None  # counted LLM tokens the recorded run spent, from /api/runs
    segments: list[Segment] = Field(min_length=1)


class ReplayMeta(BaseModel):
    """What the UI needs to offer a replay; the events come from the stream endpoint."""

    slug: str
    question: str
    recorded_at: datetime
    source: Literal["live", "scripted"]
    tokens: int | None
    inputs: list[SegmentInput]


def load_replays(directory: Path) -> dict[str, Replay]:
    """Every valid `<slug>.json` in the folder. A broken file is logged and skipped, so one
    bad fixture can't take the API down (CI validates the committed ones)."""
    found: dict[str, Replay] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        try:
            replay = Replay.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError) as exc:
            logger.error("skipped invalid replay", extra={"file": path.name, "error": str(exc)})
            continue
        if replay.slug != path.stem or not SLUG_RE.match(replay.slug):
            logger.error("skipped replay: slug must match the file name", extra={"file": path.name})
            continue
        found[replay.slug] = replay
    return found


def playback_delay_ms(prev: RecordedEvent | None, ev: RecordedEvent) -> float:
    if prev is None:
        return 0.0
    gap = max(ev.t_ms - prev.t_ms, 0) / SPEEDUP
    cap = MAX_TOKEN_GAP_MS if ev.event.event == "token" else MAX_GAP_MS
    return min(gap, cap)


async def play(events: list[RecordedEvent], speed: float = 1.0) -> AsyncIterator[bytes]:
    prev: RecordedEvent | None = None
    for i, ev in enumerate(events, start=1):
        delay = playback_delay_ms(prev, ev) * speed
        if delay > 0:
            await asyncio.sleep(delay / 1000)
        prev = ev
        yield format_sse_event(event=ev.event.event, data_str=ev.event.model_dump_json(), id=str(i))


def _replays(request: Request) -> dict[str, Replay]:
    replays: dict[str, Replay] = request.app.state.replays
    return replays


@router.get("", response_model=list[ReplayMeta])
async def list_replays(request: Request) -> list[ReplayMeta]:
    return [
        ReplayMeta(
            slug=r.slug,
            question=r.question,
            recorded_at=r.recorded_at,
            source=r.source,
            tokens=r.tokens,
            inputs=[s.input for s in r.segments],
        )
        for r in sorted(_replays(request).values(), key=lambda r: r.recorded_at)
    ]


@router.get(
    "/{slug}",
    responses={
        200: {
            "description": "SSE stream of the recorded events",
            "content": {"text/event-stream": {}},
        }
    },
)
async def stream_replay(
    slug: str, request: Request, segment: int = Query(0, ge=0)
) -> EventSourceResponse:
    """Play one segment of a recording with compressed timing. Costs no tokens and is not
    subject to the live-run limits."""
    replay = _replays(request).get(slug)
    if replay is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such recorded run.")
    if segment >= len(replay.segments):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such segment in this recording.")
    speed: float = request.app.state.settings.replay_delay_factor
    return EventSourceResponse(
        play(replay.segments[segment].events, speed),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
