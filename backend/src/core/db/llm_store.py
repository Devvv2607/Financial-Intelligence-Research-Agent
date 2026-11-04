"""Postgres implementation of `core.llm.LLMStore` (response cache + call ledger)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db.schema import llm_cache, llm_calls
from core.llm import CallRecord


class PostgresLLMStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def cache_get(self, key: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(llm_cache.c.response).where(llm_cache.c.key == key))
            ).scalar_one_or_none()
        return row

    async def cache_put(self, key: str, model: str, response: dict[str, Any]) -> None:
        stmt = insert(llm_cache).values(key=key, model=model, response=response)
        async with self._engine.begin() as conn:
            await conn.execute(stmt.on_conflict_do_nothing(index_elements=["key"]))

    async def record(self, call: CallRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                llm_calls.insert().values(
                    model=call.model,
                    stage=call.stage,
                    cik=call.cik,
                    prompt_tokens=call.prompt_tokens,
                    completion_tokens=call.completion_tokens,
                    reasoning_tokens=call.reasoning_tokens,
                    latency_ms=call.latency_ms,
                    cache_hit=call.cache_hit,
                    ok=call.ok,
                    error=call.error,
                )
            )

    async def tokens_last_24h(self, model: str) -> int:
        since = datetime.now(UTC) - timedelta(hours=24)
        stmt = select(
            func.coalesce(func.sum(llm_calls.c.prompt_tokens + llm_calls.c.completion_tokens), 0)
        ).where(llm_calls.c.model == model, llm_calls.c.created_at >= since)
        async with self._engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar_one())
