"""Run log: one `runs` row per agent run, with tokens per node from the LLM ledger."""

import uuid
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.db.llm_store import COUNTED_TOKENS
from core.db.schema import llm_calls, runs


async def node_usage(engine: AsyncEngine, run_id: str) -> dict[str, dict[str, int]]:
    """Per node: live calls, counted tokens, reasoning tokens, cache hits and what they saved."""
    stmt = (
        select(
            llm_calls.c.stage,
            func.count().filter(~llm_calls.c.cache_hit).label("calls"),
            func.coalesce(func.sum(COUNTED_TOKENS), 0).label("counted"),
            func.coalesce(func.sum(llm_calls.c.prompt_tokens), 0).label("prompt"),
            func.coalesce(func.sum(llm_calls.c.completion_tokens), 0).label("completion"),
            func.coalesce(func.sum(llm_calls.c.reasoning_tokens), 0).label("reasoning"),
            func.sum(case((llm_calls.c.cache_hit, 1), else_=0)).label("cache_hits"),
            func.coalesce(func.sum(llm_calls.c.saved_tokens), 0).label("saved"),
            func.coalesce(func.sum(llm_calls.c.latency_ms), 0).label("latency_ms"),
        )
        .where(llm_calls.c.run_id == run_id)
        .group_by(llm_calls.c.stage)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return {r["stage"]: {k: int(v or 0) for k, v in r.items() if k != "stage"} for r in rows}


def make_run_recorder(engine: AsyncEngine, settings: Settings) -> Any:
    model = settings.model_for("reasoning", agent=True)
    price_in, price_out = settings.groq_prices_usd_per_mtok.get(model, (0.0, 0.0))

    async def record(run: dict[str, Any]) -> None:
        run_id = run.get("run_id") or str(uuid.uuid4())
        usage = await node_usage(engine, run_id)
        prompt = sum(u["prompt"] for u in usage.values())
        completion = sum(u["completion"] for u in usage.values())
        stmt = insert(runs).values(
            id=uuid.UUID(run_id),
            thread_id=run.get("thread_id") or "",
            query=run["query"],
            route=run.get("route"),
            plan=run["plan"],
            steps=run["steps"],
            answer=run["answer"],
            node_usage=usage,
            citation_check=run.get("citation_check"),
            prompt_tokens=prompt,
            completion_tokens=completion,
            latency_ms=sum(u["latency_ms"] for u in usage.values()),
            cost_usd=(prompt * price_in + completion * price_out) / 1e6,
        )
        update_cols = [
            "plan",
            "steps",
            "answer",
            "node_usage",
            "citation_check",
            "prompt_tokens",
            "completion_tokens",
            "latency_ms",
            "cost_usd",
            "route",
        ]
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"], set_={c: stmt.excluded[c] for c in update_cols}
        )
        async with engine.begin() as conn:
            await conn.execute(stmt)

    return record
