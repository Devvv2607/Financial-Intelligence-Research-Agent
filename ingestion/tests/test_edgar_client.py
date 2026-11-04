"""EDGAR client: rate limiting, retries/backoff, caching, User-Agent (mocked with respx)."""

from pathlib import Path

import httpx
import pytest
import respx

from ingestion.edgar.client import EdgarClient, EdgarError, RateLimiter

URL = "https://data.sec.gov/submissions/CIK0000320193.json"
UA = "Test Person test@example.com"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(tmp_path: Path, clock: FakeClock, **kw: object) -> EdgarClient:
    return EdgarClient(
        UA, tmp_path, requests_per_second=8, http=httpx.AsyncClient(), sleep=clock.sleep, **kw
    )  # type: ignore[arg-type]


async def test_rate_limiter_spaces_requests_at_most_rate_per_second() -> None:
    clock = FakeClock()
    limiter = RateLimiter(8, clock=clock, sleep=clock.sleep)
    for _ in range(9):
        await limiter.acquire()
    # 9 acquisitions at 8/s need 8 gaps of 1/8 s = 1 s total, never faster.
    assert clock.now == pytest.approx(1.0)
    assert all(s == pytest.approx(0.125) for s in clock.sleeps)


@respx.mock
async def test_sends_user_agent_and_caches_to_disk(tmp_path: Path) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"cik": "320193"}))
    client = _client(tmp_path, FakeClock())
    assert await client.get_json(URL) == {"cik": "320193"}
    assert await client.get_json(URL) == {"cik": "320193"}  # second read: from disk
    assert route.call_count == 1
    assert route.calls[0].request.headers["User-Agent"] == UA
    assert client.cache_path(URL).exists()
    assert client.network_requests == 1


@respx.mock
async def test_refresh_bypasses_cache(tmp_path: Path) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    client = _client(tmp_path, FakeClock())
    await client.get_json(URL)
    await client.get_json(URL, refresh=True)
    assert route.call_count == 2


@respx.mock
async def test_retries_429_and_5xx_with_exponential_backoff(tmp_path: Path) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"ok": 1})]
    )
    clock = FakeClock()
    client = _client(tmp_path, clock)
    assert await client.get_json(URL) == {"ok": 1}
    assert route.call_count == 3
    backoffs = [s for s in clock.sleeps if s >= 1.0]
    # 1s then 2s base delays, each with up to +25% jitter.
    assert 1.0 <= backoffs[0] <= 1.25
    assert 2.0 <= backoffs[1] <= 2.5


@respx.mock
async def test_honors_retry_after_header(tmp_path: Path) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={}),
        ]
    )
    clock = FakeClock()
    await _client(tmp_path, clock).get_json(URL)
    assert 7.0 in clock.sleeps


@respx.mock
async def test_gives_up_after_max_retries(tmp_path: Path) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    client = _client(tmp_path, FakeClock(), max_retries=2)
    with pytest.raises(EdgarError, match="3 attempts"):
        await client.get_json(URL)
    assert route.call_count == 3
    assert not client.cache_path(URL).exists()  # failures are never cached


@respx.mock
async def test_does_not_retry_client_errors(tmp_path: Path) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(EdgarError, match="404"):
        await _client(tmp_path, FakeClock()).get_json(URL)
    assert route.call_count == 1


@respx.mock
async def test_retries_transport_errors(tmp_path: Path) -> None:
    respx.get(URL).mock(side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200, json={})])
    assert await _client(tmp_path, FakeClock()).get_json(URL) == {}


def test_requires_user_agent(tmp_path: Path) -> None:
    with pytest.raises(EdgarError, match="SEC_USER_AGENT"):
        EdgarClient("  ", tmp_path)
