"""Per-run event log (replayable SSE) and translation of LangGraph task events.

Every event of a run is appended to an in-memory `RunLog`; event *n* has SSE id *n*.
The POST that starts a run streams the log from the beginning, and
GET /api/threads/{id}/events?after=n (or `Last-Event-ID`) replays what a dropped
connection missed, then follows live. A log is kept EVENT_LOG_TTL_S after its run ends.

Trade-off (D-043): the log lives in this process, not in a table. After an API restart
the log is gone; the run itself is in Postgres (checkpointer + `runs`), so a client that
gets 404 here falls back to polling GET /api/threads/{id}. That costs the token-by-token
replay, not the content. With several API instances, a shared log is needed: Redis
Streams (XADD per event, XREAD from the last id) is the scale-up path.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from api.schemas import AgentEvent

EVENT_LOG_TTL_S = 600.0


@dataclass
class RunLog:
    run_id: str
    events: list[AgentEvent] = field(default_factory=list)
    done: bool = False
    finished_at: float | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event)

    def append(self, event: AgentEvent) -> None:
        self.events.append(event)
        self._notify()

    def close(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.done = True
        self.finished_at = clock()
        self._notify()

    def _notify(self) -> None:
        # Wake everyone waiting on the current event, then start a fresh one. There's no
        # await between a reader's check and its wait, so no wake-up can be missed.
        self._wake.set()
        self._wake = asyncio.Event()

    async def wait_after(self, after: int, wait_s: float) -> list[AgentEvent] | None:
        """Events after id `after`; waits up to `wait_s` for new ones. None = timed out
        (send a keep-alive). An empty list means the run is over and nothing is left."""
        if len(self.events) > after or self.done:
            return self.events[after:]
        wake = self._wake
        try:
            await asyncio.wait_for(wake.wait(), wait_s)
        except TimeoutError:
            return None
        return self.events[after:]

    def expired(self, now: float) -> bool:
        return self.finished_at is not None and now - self.finished_at > EVENT_LOG_TTL_S


class NodeTracker:
    """LangGraph `tasks` stream chunks -> `node` events with /api/graph node ids.

    Top-level nodes keep their name ("planner"); nodes inside a specialist subgraph are
    "<agent>:<node>" ("filings_agent:direct"), as in `get_graph(xray=1)`. Parallel
    instances of one specialist are told apart by `step_id`, taken from the specialist
    task's input and remembered by its task id (which is its subgraph namespace)."""

    def __init__(self) -> None:
        self._steps: dict[str, str] = {}

    def event(self, namespace: tuple[str, ...], chunk: dict[str, Any]) -> dict[str, Any]:
        name = str(chunk["name"])
        start = "result" not in chunk
        step_id: str | None = None
        if namespace:
            agent, _, task = namespace[0].partition(":")
            node = f"{agent}:{name}"
            step_id = self._steps.get(task)
        else:
            node = name
            step = _input_step_id(chunk.get("input")) if start else None
            if step is not None:
                self._steps[str(chunk["id"])] = step
            step_id = step or self._steps.get(str(chunk["id"]))
        out: dict[str, Any] = {
            "event": "node",
            "node": node,
            "phase": "start" if start else "end",
            "step_id": step_id,
        }
        if not start:
            out["error"] = chunk.get("error") is not None
            out["interrupted"] = bool(chunk.get("interrupts"))
        return out


def _input_step_id(value: Any) -> str | None:
    step = value.get("step") if isinstance(value, dict) else None
    sid = getattr(step, "id", None)
    return str(sid) if sid is not None else None
