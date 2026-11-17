"""One live run at a time, first come first served, with a visible queue position.

Groq's free tier allows 8K tokens per minute on the agent model, and one run can use most
of that in its first minute, so two runs in parallel both hit 429s and both get slower.
Runs therefore wait for a slot (LIVE_CONCURRENCY, default 1); each waiting run is told
its position whenever it changes (a `queued` event), and a request that would make the
queue longer than LIVE_QUEUE_MAX is refused before it starts (`queue-full`).
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

OnPosition = Callable[[int], None]


@dataclass
class _Waiter:
    granted: asyncio.Future[None]
    on_position: OnPosition


@dataclass
class RunGate:
    slots: int = 1
    max_waiting: int = 5
    _running: int = 0
    _waiters: list[_Waiter] = field(default_factory=list)

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def would_queue_overflow(self) -> bool:
        """True if one more run would have to wait and the queue is already full."""
        return self._running >= self.slots and len(self._waiters) >= self.max_waiting

    @asynccontextmanager
    async def slot(self, on_position: OnPosition) -> AsyncIterator[None]:
        """Hold a run slot for the body; while waiting, `on_position(n)` reports the place
        in the queue (1 = next)."""
        if self._running < self.slots and not self._waiters:
            self._running += 1
        else:
            waiter = _Waiter(asyncio.get_running_loop().create_future(), on_position)
            self._waiters.append(waiter)
            on_position(len(self._waiters))
            try:
                await waiter.granted  # the releasing run counts us in before waking us
            except asyncio.CancelledError:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._announce()
                elif waiter.granted.done() and not waiter.granted.cancelled():
                    self._release()  # granted just as we were cancelled: pass it on
                raise
        try:
            yield
        finally:
            self._release()

    def _release(self) -> None:
        self._running -= 1
        while self._waiters and self._running < self.slots:
            nxt = self._waiters.pop(0)
            self._running += 1
            nxt.granted.set_result(None)
        self._announce()

    def _announce(self) -> None:
        for i, w in enumerate(self._waiters, start=1):
            w.on_position(i)
