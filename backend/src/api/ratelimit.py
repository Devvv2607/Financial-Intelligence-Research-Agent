"""Per-IP sliding-window limit on agent runs, to protect the public demo's Groq quota.

In memory and per process: enough for one API instance. Behind a proxy in Phase 7 the
client address must come from the proxy's forwarded header (configured in uvicorn with
--proxy-headers / --forwarded-allow-ips), not from here.
"""

import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status

WINDOW_S = 60.0


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def retry_after(self, key: str) -> int | None:
        """Record a hit; return seconds to wait if over the limit (the hit is not recorded)."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= WINDOW_S:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, math.ceil(WINDOW_S - (now - hits[0])))
        hits.append(now)
        if len(self._hits) > 10_000:  # forget idle clients
            self._hits = {k: v for k, v in self._hits.items() if v}
        return None


def limit_runs(request: Request) -> None:
    """FastAPI dependency for endpoints that start (or resume) an agent run."""
    limiter: RateLimiter = request.app.state.rate_limiter
    key = request.client.host if request.client else "unknown"
    wait = limiter.retry_after(key)
    if wait is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many research runs from this address; try again in {wait}s.",
            headers={"Retry-After": str(wait)},
        )
