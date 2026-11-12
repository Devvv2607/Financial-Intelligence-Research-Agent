"""Thread store contract, run against both stores: updated_at moves on every change.

The Postgres variant needs the local stack (skips otherwise) and deletes only the rows
it created, by id.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import get_args

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from core.config import Settings
from core.db import create_engine
from core.db.schema import THREAD_STATUSES, threads
from core.threads import (
    MemoryThreadStore,
    PostgresThreadStore,
    ThreadStatus,
    ThreadStore,
    title_from,
)


def test_status_literal_matches_the_check_constraint() -> None:
    assert get_args(ThreadStatus) == THREAD_STATUSES


def test_title_is_shortened_at_a_word() -> None:
    assert title_from("  How   risky\nis JPM? ") == "How risky is JPM?"
    long = title_from("word " * 40)
    assert len(long) <= 80 and long.endswith("…")


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ThreadStore]:
    if request.param == "memory":
        yield MemoryThreadStore()
        return
    engine = create_engine(Settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM threads LIMIT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"local stack (with the threads table) not reachable: {type(exc).__name__}")
    pg = _TrackingStore(engine)
    yield pg
    async with engine.begin() as conn:  # only the rows this test created
        await conn.execute(delete(threads).where(threads.c.id.in_(pg.created)))
    await engine.dispose()


class _TrackingStore(PostgresThreadStore):
    def __init__(self, engine: object) -> None:
        super().__init__(engine)  # type: ignore[arg-type]
        self.created: list[uuid.UUID] = []

    async def create(self, title: str | None):  # type: ignore[no-untyped-def]
        t = await super().create(title)
        self.created.append(t.id)
        return t

    async def ensure(self, thread_id: uuid.UUID, title: str):  # type: ignore[no-untyped-def]
        self.created.append(thread_id)
        return await super().ensure(thread_id, title)


async def test_every_status_change_moves_updated_at(store: ThreadStore) -> None:
    t0 = await store.create(None)
    assert t0.status == "idle" and t0.updated_at >= t0.created_at
    stamps = [t0.updated_at]
    run = uuid.uuid4()
    for status in ("running", "awaiting_clarification", "running", "idle"):
        await asyncio.sleep(0.002)  # distinct timestamps
        await store.set_status(t0.id, status, run if status == "running" else None)  # type: ignore[arg-type]
        t = await store.get(t0.id)
        assert t is not None and t.status == status
        assert t.updated_at > stamps[-1], status
        assert t.created_at == t0.created_at
        stamps.append(t.updated_at)
    assert t.last_run_id == run  # kept when a later change passes no run id

    await asyncio.sleep(0.002)
    await store.set_title_if_empty(t0.id, "First question")
    titled = await store.get(t0.id)
    assert titled is not None and titled.title == "First question"
    assert titled.updated_at > stamps[-1]
    assert (await store.recent(5))[0].id == t0.id  # most recently updated first


async def test_ensure_registers_once_and_keeps_the_first_title(store: ThreadStore) -> None:
    tid = uuid.uuid4()
    first = await store.ensure(tid, "How risky is the bank?")
    again = await store.ensure(tid, "a later question")
    assert first.id == again.id == tid
    assert again.title == "How risky is the bank?"
    assert again.created_at == first.created_at


@pytest.mark.integration
async def test_database_rejects_an_unknown_status() -> None:
    engine = create_engine(Settings().database_url)
    tid = uuid.uuid4()
    try:
        try:
            async with engine.begin() as conn:
                await conn.execute(threads.insert().values(id=tid, title="t"))
        except Exception as exc:
            pytest.skip(f"local stack not reachable: {type(exc).__name__}")
        with pytest.raises(IntegrityError, match="ck_threads_status"):
            async with engine.begin() as conn:
                await conn.execute(
                    threads.update().where(threads.c.id == tid).values(status="paused")
                )
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(threads).where(threads.c.id == tid))
        await engine.dispose()
