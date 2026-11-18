"""Weaviate client lifecycle (D-053): one client per process that connects itself and
reconnects once when it finds itself closed or disconnected.

The production bug of 2026-11-18: the API's client was created but never connected
(only /ready connected it), so after a deploy every query raised
WeaviateClosedClientError until someone opened /ready.
"""

import asyncio
from typing import Any

import pytest
from weaviate.exceptions import WeaviateClosedClientError, WeaviateConnectionError

from core import vectorstore
from core.config import Settings
from core.vectorstore import FILING_CHUNK, count_objects, create_client, with_reconnect


class FakeClient:
    """Enough of WeaviateAsyncClient: connection state, connect/close counters."""

    def __init__(self, connected: bool = False) -> None:
        self.connected = connected
        self.connects = 0
        self.closes = 0

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        await asyncio.sleep(0)
        self.connects += 1
        self.connected = True

    async def close(self) -> None:
        self.closes += 1
        self.connected = False


def failing_then_ok(errors: list[Exception]) -> Any:
    calls: list[int] = []

    async def op(client: FakeClient) -> str:
        calls.append(1)
        if not client.connected:
            raise WeaviateClosedClientError()
        if errors:
            raise errors.pop(0)
        return "ok"

    op.calls = calls  # type: ignore[attr-defined]
    return op


async def test_a_never_connected_client_connects_before_the_first_query() -> None:
    client = FakeClient(connected=False)  # the production state after a deploy
    op = failing_then_ok([])
    assert await with_reconnect(client, op) == "ok"  # type: ignore[arg-type]
    assert (client.connects, client.closes, len(op.calls)) == (1, 0, 1)


@pytest.mark.parametrize(
    "error", [WeaviateClosedClientError(), WeaviateConnectionError("connection reset")]
)
async def test_a_dropped_connection_reconnects_once_and_retries_once(error: Exception) -> None:
    client = FakeClient(connected=True)
    op = failing_then_ok([error])
    assert await with_reconnect(client, op) == "ok"  # type: ignore[arg-type]
    assert (client.closes, client.connects, len(op.calls)) == (1, 1, 2)


async def test_only_one_retry_then_the_error_surfaces() -> None:
    client = FakeClient(connected=True)
    op = failing_then_ok([WeaviateConnectionError("down"), WeaviateConnectionError("still down")])
    with pytest.raises(WeaviateConnectionError, match="still down"):
        await with_reconnect(client, op)  # type: ignore[arg-type]
    assert len(op.calls) == 2


async def test_other_errors_are_not_retried() -> None:
    client = FakeClient(connected=True)
    op = failing_then_ok([ValueError("bad filter")])
    with pytest.raises(ValueError, match="bad filter"):
        await with_reconnect(client, op)  # type: ignore[arg-type]
    assert (client.closes, len(op.calls)) == (0, 1)


async def test_concurrent_failures_reconnect_the_client_once() -> None:
    client = FakeClient(connected=True)
    broken = {"yes": True}

    async def op(c: FakeClient) -> str:
        await asyncio.sleep(0)
        if broken["yes"] and c.connects == 0:
            raise WeaviateConnectionError("connection reset")
        return "ok"

    results = await asyncio.gather(*(with_reconnect(client, op) for _ in range(5)))  # type: ignore[arg-type]
    assert results == ["ok"] * 5
    assert (client.closes, client.connects) == (1, 1)


# ---------------------------------------------------------------- the real thing
@pytest.mark.integration
async def test_real_client_queries_unconnected_and_after_close() -> None:
    """Against the local Weaviate: exactly the production failure, then a closed client."""
    probe = create_client(Settings())
    try:
        await asyncio.wait_for(vectorstore.ensure_ready(probe), 5)
    except Exception as exc:
        pytest.skip(f"local Weaviate not reachable: {type(exc).__name__}")
    finally:
        await probe.close()
    client = create_client(Settings())
    try:
        assert not client.is_connected()  # created, never connected: the bug's state
        first = await count_objects(client, FILING_CHUNK)
        assert first >= 0 and client.is_connected()

        await client.close()  # closed under us (shutdown race, manual close, ...)
        assert await count_objects(client, FILING_CHUNK) == first
    finally:
        await client.close()
