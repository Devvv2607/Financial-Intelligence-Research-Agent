"""Repo-wide pytest setup."""

from collections.abc import Callable, Mapping
from typing import Any

from core.aio import selector_loop


def pytest_asyncio_loop_factories(config: Any, item: Any) -> Mapping[str, Callable[[], Any]]:
    # Async psycopg can't run on Windows' default Proactor loop (DECISIONS D-016), so
    # every async test gets the same selector-loop factory the CLIs use. On Linux/CI this
    # is a plain new event loop.
    return {"selector": selector_loop}
