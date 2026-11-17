"""Live-run limits that protect the public demo's free Groq quota.

- `RateLimiter`: a per-client sliding window (API_RUNS_PER_MINUTE) on runs and resumes.
- `DailyCap`: live questions per UTC day, counted per visitor id AND per client IP
  (LIVE_RUNS_PER_VISITOR_PER_DAY, LIVE_RUNS_PER_IP_PER_DAY). A new browser id alone
  doesn't reset it (the IP still counts), and visitors sharing an office IP each keep
  their own visitor count up to the IP's. Resumes don't count: they finish a question.

Both are in memory, for one API process: a restart forgets today's counts. That is the
accepted trade-off for a single free instance; the hard ceilings are the agent's daily
token budget (AGENT_DAILY_TOKEN_BUDGET) and the one-run-at-a-time gate.

The client IP: behind Railway's edge, `request.client` is the proxy, so every visitor
would share one key. Railway documents `X-Real-IP` as the client's address (Specs &
Limits, checked 2026-11-17), but not whether it replaces a client-sent copy, so the
header is read only when CLIENT_IP_HEADER names it, and the IP is only one of two keys.
IPs are kept only as a hash, in memory, and never logged.
"""

import hashlib
import math
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime

from fastapi import HTTPException, Request, status

from core.config import Settings

WINDOW_S = 60.0
LIMIT_HEADER = "X-Limit-Reason"  # tells the UI which limit refused the run


def client_ip(request: Request) -> str:
    settings: Settings = request.app.state.settings
    if settings.client_ip_header:
        value = request.headers.get(settings.client_ip_header, "")
        first = value.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _ip_key(ip: str) -> str:
    return "ip:" + hashlib.sha256(ip.encode()).hexdigest()[:24]


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
    wait = limiter.retry_after(_ip_key(client_ip(request)))
    if wait is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many research runs from this address; try again in {wait}s.",
            headers={"Retry-After": str(wait), LIMIT_HEADER: "per-minute"},
        )


class DailyCap:
    def __init__(
        self,
        per_visitor: int,
        per_ip: int,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self._limits = {"v": per_visitor, "ip": per_ip}
        self._today = today
        self._day = today()
        self._counts: dict[str, int] = {}

    def _keys(self, visitor: str, ip: str) -> dict[str, str]:
        day = self._today()
        if day != self._day:  # a new UTC day: everyone starts again
            self._day, self._counts = day, {}
        return {"v": "v:" + visitor, "ip": _ip_key(ip)}

    def allowed(self, visitor: str, ip: str) -> bool:
        keys = self._keys(visitor, ip)
        return all(self._counts.get(keys[k], 0) < self._limits[k] for k in keys)

    def take(self, visitor: str, ip: str) -> bool:
        """Count one live run if both keys are under their cap. Check and count with no
        await in between, so concurrent requests can't both take the last run."""
        if not self.allowed(visitor, ip):
            return False
        for key in self._keys(visitor, ip).values():
            self._counts[key] = self._counts.get(key, 0) + 1
        return True

    @property
    def per_visitor(self) -> int:
        return self._limits["v"]


def cap_reached(cap: DailyCap) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"You've used today's {cap.per_visitor} live questions. This demo runs on a free "
            "API quota, so live runs are capped per visitor; the cap resets at 00:00 UTC. "
            "The recorded runs still play instantly."
        ),
        headers={LIMIT_HEADER: "daily-cap"},
    )
