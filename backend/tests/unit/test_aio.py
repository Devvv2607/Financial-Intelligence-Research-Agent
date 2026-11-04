import asyncio
import sys

import pytest

from core import aio


def test_selector_loop_is_psycopg_compatible() -> None:
    loop = aio.selector_loop()
    try:
        # psycopg async refuses the ProactorEventLoop (Windows default).
        assert not type(loop).__name__.startswith("Proactor")
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_run_executes_coroutine_on_selector_loop() -> None:
    async def which_loop() -> str:
        return type(asyncio.get_running_loop()).__name__

    assert not aio.run(which_loop()).startswith("Proactor")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only default loop behaviour")
def test_windows_default_loop_would_break_psycopg() -> None:
    # Documents *why* core.aio exists: the stock asyncio.run loop on Windows is Proactor.
    async def which_loop() -> str:
        return type(asyncio.get_running_loop()).__name__

    assert asyncio.run(which_loop()) == "ProactorEventLoop"
