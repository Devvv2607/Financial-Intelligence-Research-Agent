"""The one-live-run gate: order, queue positions, cancellation."""

import asyncio

from api.gate import RunGate


async def test_runs_wait_in_order_and_hear_their_position() -> None:
    gate = RunGate(slots=1, max_waiting=5)
    order: list[str] = []
    positions: dict[str, list[int]] = {"b": [], "c": []}
    release_a = asyncio.Event()

    async def run(name: str, hold: asyncio.Event | None = None) -> None:
        async with gate.slot(lambda n: positions[name].append(n)):
            order.append(name)
            if hold:
                await hold.wait()

    a = asyncio.create_task(run("a", release_a))
    await asyncio.sleep(0)
    b = asyncio.create_task(run("b"))
    await asyncio.sleep(0)
    c = asyncio.create_task(run("c"))
    await asyncio.sleep(0)
    assert order == ["a"] and gate.waiting == 2
    assert positions == {"b": [1], "c": [2]}
    release_a.set()
    await asyncio.gather(a, b, c)
    assert order == ["a", "b", "c"]
    assert positions["c"][-1] == 1  # moved up when a finished
    assert gate.waiting == 0 and not gate.would_queue_overflow()


async def test_a_cancelled_waiter_leaves_the_queue_and_the_rest_move_up() -> None:
    gate = RunGate(slots=1, max_waiting=1)
    hold = asyncio.Event()
    seen: list[int] = []

    async def holder() -> None:
        async with gate.slot(lambda n: None):
            await hold.wait()

    async def waiter(log: list[int]) -> None:
        async with gate.slot(log.append):
            pass

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    w = asyncio.create_task(waiter(seen))
    await asyncio.sleep(0)
    assert gate.would_queue_overflow()  # one running, one waiting, max_waiting=1
    w.cancel()
    await asyncio.gather(w, return_exceptions=True)
    assert gate.waiting == 0 and not gate.would_queue_overflow()
    hold.set()
    await h
    async with gate.slot(lambda n: None):  # the slot was released properly
        pass
