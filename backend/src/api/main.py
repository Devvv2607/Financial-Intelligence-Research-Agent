"""FastAPI app factory.

Long-lived clients (DB engine, Weaviate) and the agent runtime (compiled graph,
checkpointer, embedding model) are created once in the lifespan and stored on
`app.state`, so every request reuses them. Tests pass their own `runtime_factory`
(scripted LLM, fake tools, in-memory checkpointer) or none at all.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.ratelimit import RateLimiter
from api.routes import data, health, threads
from api.runtime import RuntimeFactory, live_runtime
from core import db, vectorstore
from core.config import Settings, get_settings
from core.logging import configure_logging


def create_app(
    settings: Settings | None = None, runtime_factory: RuntimeFactory | None = live_runtime
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.rate_limiter = RateLimiter(settings.api_runs_per_minute)
        app.state.engine = db.create_engine(settings.database_url)
        app.state.weaviate = vectorstore.create_client(settings)
        app.state.runtime = None
        try:
            if runtime_factory is None:
                yield
            else:
                async with runtime_factory(settings, app.state.engine, app.state.weaviate) as rt:
                    app.state.runtime = rt
                    yield
        finally:
            await app.state.weaviate.close()
            await app.state.engine.dispose()

    app = FastAPI(title="Financial Intelligence Research Agent", version="0.4.0", lifespan=lifespan)
    # Locked to the frontend origin(s); credentials not needed (no cookies).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(threads.router)
    app.include_router(data.router)
    return app


app = create_app()
