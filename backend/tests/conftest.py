"""Shared by every backend test: the suite never calls Groq."""

from collections.abc import Iterator

import httpx
import pytest
import respx


@pytest.fixture(autouse=True)
def no_groq(request: pytest.FixtureRequest) -> Iterator[None]:
    """Zero Groq calls in the test suite: any request to api.groq.com fails the test.
    Everything else (local Postgres/Weaviate in integration tests) passes through.
    Tests marked `groq_mocked` serve api.groq.com from their own respx mock instead."""
    if request.node.get_closest_marker("groq_mocked"):
        yield
        return

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"test tried to call Groq: {request.url}")

    with respx.mock(assert_all_called=False) as router:
        router.route(host="api.groq.com").mock(side_effect=refuse)
        router.route().pass_through()
        yield
