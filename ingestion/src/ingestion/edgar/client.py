"""SEC EDGAR HTTP client: global rate limit, retries with backoff, on-disk cache.

SEC allows 10 requests/second per client and requires a descriptive User-Agent.
We stay at <= 8 req/s across *all* concurrent tasks (one shared limiter), back off
exponentially on 429/5xx, and cache every successful response under data/raw/ so a
re-run never re-downloads (BUILD_SPEC 3.2).
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class RateLimiter:
    """Evenly spaced requests: at most `rate` acquisitions per second, shared by all tasks."""

    def __init__(
        self,
        rate: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_at - now
            if wait > 0:
                await self._sleep(wait)
            self._next_at = max(now, self._next_at) + self._interval


class EdgarError(RuntimeError):
    pass


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        requests_per_second: float = 8.0,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 5,
        backoff_base_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not user_agent.strip():
            raise EdgarError("SEC_USER_AGENT is required (SEC rejects anonymous clients)")
        self._cache_dir = cache_dir
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True
        )
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._limiter = RateLimiter(requests_per_second, sleep=sleep)
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._sleep = sleep
        self.network_requests = 0  # for reporting: how many calls actually hit SEC

    async def aclose(self) -> None:
        await self._http.aclose()

    def cache_path(self, url: str) -> Path:
        parsed = urlparse(url)
        path = parsed.path.lstrip("/") or "index"
        return self._cache_dir / "sec" / parsed.netloc / path

    async def get_bytes(self, url: str, *, refresh: bool = False) -> bytes:
        path = self.cache_path(url)
        if path.exists() and not refresh:
            return path.read_bytes()
        content = await self._fetch(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(content)
        tmp.replace(path)  # atomic: a crash never leaves a half-written cache entry
        return content

    async def get_json(self, url: str, *, refresh: bool = False) -> Any:
        return json.loads(await self.get_bytes(url, refresh=refresh))

    async def get_text(self, url: str, *, refresh: bool = False) -> str:
        return (await self.get_bytes(url, refresh=refresh)).decode("utf-8", errors="replace")

    async def _fetch(self, url: str) -> bytes:
        last_error = ""
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            self.network_requests += 1
            try:
                resp = await self._http.get(url, headers=self._headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}"
            else:
                if resp.status_code == 200:
                    return resp.content
                if resp.status_code not in RETRY_STATUSES:
                    raise EdgarError(f"GET {url} -> HTTP {resp.status_code}")
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.isdigit():
                    await self._sleep(float(retry_after))
                    continue
            if attempt < self._max_retries:
                # Exponential backoff with jitter: 1s, 2s, 4s, ... (+0-25%).
                delay = self._backoff_base_s * 2**attempt
                await self._sleep(delay * (1 + random.random() / 4))  # noqa: S311
        raise EdgarError(f"GET {url} failed after {self._max_retries + 1} attempts: {last_error}")
