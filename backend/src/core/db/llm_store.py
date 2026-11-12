"""Postgres implementation of `core.llm.LLMStore` (response cache + call ledger)."""

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db.schema import llm_cache, llm_calls
from core.llm import CachedResponse, CallRecord

# Tokens that count toward Groq rate limits: cached prompt tokens are excluded.
COUNTED_TOKENS = (
    llm_calls.c.prompt_tokens - llm_calls.c.cached_tokens + llm_calls.c.completion_tokens
)


class PostgresLLMStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def cache_get(self, key: str) -> CachedResponse | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(llm_cache.c.response, llm_cache.c.counted_tokens).where(
                        llm_cache.c.key == key
                    )
                )
            ).one_or_none()
        return CachedResponse(row.response, row.counted_tokens or 0) if row else None

    async def cache_put(
        self, key: str, model: str, response: dict[str, Any], counted_tokens: int
    ) -> None:
        stmt = insert(llm_cache).values(
            key=key, model=model, response=response, counted_tokens=counted_tokens
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt.on_conflict_do_nothing(index_elements=["key"]))

    async def record(self, call: CallRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                llm_calls.insert().values(
                    model=call.model,
                    stage=call.stage,
                    cik=call.cik,
                    run_id=call.run_id,
                    prompt_tokens=call.prompt_tokens,
                    cached_tokens=call.cached_tokens,
                    completion_tokens=call.completion_tokens,
                    reasoning_tokens=call.reasoning_tokens,
                    saved_tokens=call.saved_tokens,
                    latency_ms=call.latency_ms,
                    cache_hit=call.cache_hit,
                    ok=call.ok,
                    error=call.error,
                )
            )

    async def tokens_last_24h(self, model: str) -> int:
        # The database's clock, like created_at itself (D-042).
        stmt = select(func.coalesce(func.sum(COUNTED_TOKENS), 0)).where(
            llm_calls.c.model == model,
            llm_calls.c.created_at >= func.now() - timedelta(hours=24),
        )
        async with self._engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar_one())
