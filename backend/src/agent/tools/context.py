"""Build and tear down the ToolContext (shared clients for every tool call)."""

from agent.tools.base import ToolContext
from core.config import Settings
from core.db import create_engine
from core.db.llm_store import PostgresLLMStore
from core.embeddings import Embedder
from core.llm import GroqClient
from core.newsapi import NewsApiClient
from core.vectorstore import create_client, ensure_ready


async def build_context(settings: Settings, *, with_llm: bool = True) -> ToolContext:
    engine = create_engine(settings.database_url)
    wv = create_client(settings)
    await ensure_ready(wv)
    news = (
        NewsApiClient(
            settings.newsapi_key.get_secret_value(), settings.newsapi_base_url, settings.raw_dir
        )
        if settings.newsapi_key and settings.news_live_fallback_enabled
        else None
    )
    # agent=True: tools use AGENT_MODEL_* so they never spend the ingestion model's
    # daily quota during the multi-day full run (D-027).
    llm = GroqClient(settings, PostgresLLMStore(engine), agent=True) if with_llm else None
    return ToolContext(
        settings=settings,
        engine=engine,
        weaviate=wv,
        embedder=Embedder(settings.embedding_model, settings.models_dir),
        llm=llm,
        news=news,
    )


async def close_context(ctx: ToolContext) -> None:
    for client in (ctx.llm, ctx.news):
        if client is not None:
            await client.aclose()
    if ctx.weaviate is not None:
        await ctx.weaviate.close()
    await ctx.engine.dispose()
