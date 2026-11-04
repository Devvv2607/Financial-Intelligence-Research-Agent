"""Event-loop helpers so async psycopg works on every OS.

Why: on Windows, asyncio defaults to the ProactorEventLoop, which psycopg 3's async
mode cannot use ("Psycopg cannot use the 'ProactorEventLoop'"). Anything that runs
on the host (uvicorn in dev, CLIs like `python -m ingestion`) must use a selector
loop there. In Linux containers this is a no-op.
"""

import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any


def selector_loop() -> asyncio.AbstractEventLoop:
    """Loop factory for `asyncio.run(loop_factory=...)` and `uvicorn --loop`."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run` with a psycopg-compatible loop. Use this in every CLI entrypoint."""
    return asyncio.run(coro, loop_factory=selector_loop)
