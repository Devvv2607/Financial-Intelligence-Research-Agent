"""Thread registry: id, title, status. Postgres in the app, in memory in tests."""

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from api.schemas import Thread, ThreadStatus
from core.db.schema import runs, threads

TITLE_CHARS = 80


def title_from(query: str) -> str:
    q = " ".join(query.split())
    return q if len(q) <= TITLE_CHARS else q[: TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"


class ThreadStore(Protocol):
    async def create(self, title: str | None) -> Thread: ...
    async def get(self, thread_id: uuid.UUID) -> Thread | None: ...
    async def recent(self, limit: int) -> list[Thread]: ...
    async def set_title_if_empty(self, thread_id: uuid.UUID, title: str) -> None: ...
    async def set_status(
        self, thread_id: uuid.UUID, status: ThreadStatus, run_id: uuid.UUID | None = None
    ) -> None: ...
    async def run_ids(self, thread_id: uuid.UUID) -> list[uuid.UUID]: ...


class PostgresThreadStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, title: str | None) -> Thread:
        stmt = insert(threads).values(id=uuid.uuid4(), title=title or "").returning(threads)
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).mappings().one()
        return Thread.model_validate(dict(row))

    async def get(self, thread_id: uuid.UUID) -> Thread | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(select(threads).where(threads.c.id == thread_id)))
                .mappings()
                .one_or_none()
            )
        return Thread.model_validate(dict(row)) if row else None

    async def recent(self, limit: int) -> list[Thread]:
        stmt = select(threads).order_by(threads.c.updated_at.desc()).limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [Thread.model_validate(dict(r)) for r in rows]

    async def set_title_if_empty(self, thread_id: uuid.UUID, title: str) -> None:
        stmt = (
            update(threads)
            .where(threads.c.id == thread_id, threads.c.title == "")
            .values(title=title)
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def set_status(
        self, thread_id: uuid.UUID, status: ThreadStatus, run_id: uuid.UUID | None = None
    ) -> None:
        values: dict[str, object] = {"status": status, "updated_at": datetime.now(UTC)}
        if run_id is not None:
            values["last_run_id"] = run_id
        async with self._engine.begin() as conn:
            await conn.execute(update(threads).where(threads.c.id == thread_id).values(**values))

    async def run_ids(self, thread_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(runs.c.id).where(runs.c.thread_id == str(thread_id)).order_by(runs.c.created_at)
        )
        async with self._engine.connect() as conn:
            return list((await conn.execute(stmt)).scalars().all())


class MemoryThreadStore:
    """Same behaviour, no database (tests). `recorded`: the fake run recorder's list."""

    def __init__(self, recorded: list[dict[str, Any]] | None = None) -> None:
        self._threads: dict[uuid.UUID, Thread] = {}
        self._recorded = recorded if recorded is not None else []

    async def run_ids(self, thread_id: uuid.UUID) -> list[uuid.UUID]:
        return [uuid.UUID(r["run_id"]) for r in self._recorded if r["thread_id"] == str(thread_id)]

    async def create(self, title: str | None) -> Thread:
        now = datetime.now(UTC)
        t = Thread(
            id=uuid.uuid4(), title=title or "", status="idle", created_at=now, updated_at=now
        )
        self._threads[t.id] = t
        return t

    async def get(self, thread_id: uuid.UUID) -> Thread | None:
        return self._threads.get(thread_id)

    async def recent(self, limit: int) -> list[Thread]:
        return sorted(self._threads.values(), key=lambda t: t.updated_at, reverse=True)[:limit]

    async def set_title_if_empty(self, thread_id: uuid.UUID, title: str) -> None:
        t = self._threads[thread_id]
        if not t.title:
            self._threads[thread_id] = t.model_copy(update={"title": title})

    async def set_status(
        self, thread_id: uuid.UUID, status: ThreadStatus, run_id: uuid.UUID | None = None
    ) -> None:
        t = self._threads[thread_id]
        upd: dict[str, object] = {"status": status, "updated_at": datetime.now(UTC)}
        if run_id is not None:
            upd["last_run_id"] = run_id
        self._threads[thread_id] = t.model_copy(update=upd)
