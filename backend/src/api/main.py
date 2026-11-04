"""FastAPI app factory.

Long-lived clients (DB engine, Weaviate) are created once in the lifespan and
stored on `app.state`, so every request reuses the same pools.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import health
from core import db, vectorstore
from core.config import Settings, get_settings
from core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = db.create_engine(settings.database_url)
        app.state.weaviate = vectorstore.create_client(settings)
        try:
            yield
        finally:
            await app.state.weaviate.close()
            await app.state.engine.dispose()

    app = FastAPI(title="Financial Intelligence Research Agent", version="0.1.0", lifespan=lifespan)
    # Locked to the frontend origin(s); credentials not needed (no cookies).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    return app


app = create_app()
