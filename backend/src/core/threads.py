"""Thread registry: id, title, status. Shared by the API and the agent CLI.

The conversation itself lives in the LangGraph checkpointer; this only lists threads
(for the UI) and tracks their status. `updated_at` has a server default for INSERT only,
so every update here sets it explicitly (one helper, `_values`, for both stores),
from the database's clock in Postgres.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db.schema import runs, threads

# Must match core.db.schema.THREAD_STATUSES (the CHECK constraint); a test enforces it.
ThreadStatus = Literal["idle", "running", "awaiting_clarification", "error"]
TITLE_CHARS = 80


class Thread(BaseModel):
    id: uuid.UUID
    title: str
    status: ThreadStatus
    last_run_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


def title_from(query: str) -> str:
    q = " ".join(query.split())
    return q if len(q) <= TITLE_CHARS else q[: TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"


def _values(clock: Any, **changes: Any) -> dict[str, Any]:
    """Every update goes through here, so updated_at can't be forgotten. Postgres passes
    `func.now()`: the same clock as the INSERT default. Mixing in the host's clock made
    updated_at go backwards (the Docker VM's clock ran ~9 ms ahead; caught by a test)."""
    return changes | {"updated_at": clock}


class ThreadStore(Protocol):
    async def create(self, title: str | None) -> Thread: ...
    async def ensure(self, thread_id: uuid.UUID, title: str) -> Thread: ...
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

    async def ensure(self, thread_id: uuid.UUID, title: str) -> Thread:
        """Register a thread id created elsewhere (the CLI); no-op if it exists."""
        stmt = pg_insert(threads).values(id=thread_id, title=title).on_conflict_do_nothing()
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
        await self.set_title_if_empty(thread_id, title)
        thread = await self.get(thread_id)
        assert thread is not None
        return thread

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
            .values(**_values(func.now(), title=title))
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def set_status(
        self, thread_id: uuid.UUID, status: ThreadStatus, run_id: uuid.UUID | None = None
    ) -> None:
        changes: dict[str, Any] = {"status": status}
        if run_id is not None:
            changes["last_run_id"] = run_id
        stmt = (
            update(threads)
            .where(threads.c.id == thread_id)
            .values(**_values(func.now(), **changes))
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

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

    async def create(self, title: str | None) -> Thread:
        return await self.ensure(uuid.uuid4(), title or "")

    async def ensure(self, thread_id: uuid.UUID, title: str) -> Thread:
        if thread_id not in self._threads:
            now = datetime.now(UTC)
            self._threads[thread_id] = Thread(
                id=thread_id, title=title, status="idle", created_at=now, updated_at=now
            )
        await self.set_title_if_empty(thread_id, title)
        return self._threads[thread_id]

    async def get(self, thread_id: uuid.UUID) -> Thread | None:
        return self._threads.get(thread_id)

    async def recent(self, limit: int) -> list[Thread]:
        return sorted(self._threads.values(), key=lambda t: t.updated_at, reverse=True)[:limit]

    async def set_title_if_empty(self, thread_id: uuid.UUID, title: str) -> None:
        t = self._threads[thread_id]
        if not t.title and title:
            self._threads[thread_id] = t.model_copy(update=_values(datetime.now(UTC), title=title))

    async def set_status(
        self, thread_id: uuid.UUID, status: ThreadStatus, run_id: uuid.UUID | None = None
    ) -> None:
        changes: dict[str, Any] = {"status": status}
        if run_id is not None:
            changes["last_run_id"] = run_id
        self._threads[thread_id] = self._threads[thread_id].model_copy(
            update=_values(datetime.now(UTC), **changes)
        )

    async def run_ids(self, thread_id: uuid.UUID) -> list[uuid.UUID]:
        return [uuid.UUID(r["run_id"]) for r in self._recorded if r["thread_id"] == str(thread_id)]
