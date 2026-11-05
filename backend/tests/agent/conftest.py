"""Fixtures for integration tests against the local stack (Postgres + Weaviate).

They run only when the stores are reachable and contain at least one ingested company,
and they use whatever companies are there (the sample or the full universe). In CI,
where no stack runs, they skip. No test here calls Groq: the context has no LLM.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from agent.tools import ToolContext, build_context, close_context
from core.config import Settings


@pytest.fixture
async def live_ctx() -> AsyncIterator[ToolContext]:
    settings = Settings()
    if not settings.models_dir.exists():
        pytest.skip("embedding model not downloaded")
    try:
        ctx = await build_context(settings, with_llm=False)
        async with ctx.engine.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM risk_factors"))).scalar_one()
    except Exception as exc:  # stack not running
        pytest.skip(f"local stack not reachable: {type(exc).__name__}")
    if not n:
        await close_context(ctx)
        pytest.skip("no ingested companies")
    yield ctx
    await close_context(ctx)
