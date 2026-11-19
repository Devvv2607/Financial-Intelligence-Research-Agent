"""FastAPI app factory.

Long-lived clients (DB engine, Weaviate) and the agent runtime (compiled graph,
checkpointer, embedding model) are created once in the lifespan and stored on
`app.state`, so every request reuses them. Tests pass their own `runtime_factory`
(scripted LLM, fake tools, in-memory checkpointer) or none at all.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.ratelimit import LIMIT_HEADER, DailyCap, RateLimiter
from api.replays import load_replays
from api.replays import router as replays_router
from api.routes import data, health, threads
from api.runtime import RuntimeFactory, live_runtime
from api.scripted import scripted_runtime
from core import db, vectorstore
from core.config import Settings, get_settings
from core.logging import configure_logging

FROM_SETTINGS: Literal["from_settings"] = "from_settings"
logger = logging.getLogger(__name__)


async def _connect_weaviate(client: Any, settings: Settings) -> None:
    """Connect at startup (D-053). Best effort: if Weaviate is down the API still runs,
    /ready reports it, and the first query connects (vectorstore.with_reconnect)."""
    try:
        await asyncio.wait_for(vectorstore.connect(client), settings.readiness_timeout_s * 2)
    except Exception as exc:
        logger.warning("weaviate not connected at startup", extra={"error": repr(exc)})


def create_app(
    settings: Settings | None = None,
    runtime_factory: RuntimeFactory | Literal["from_settings"] | None = FROM_SETTINGS,
) -> FastAPI:
    """`runtime_factory`: by default the live agent, or the scripted one when
    AGENT_RUNTIME=scripted; None = no agent (health checks, contract export)."""
    settings = settings or get_settings()
    if runtime_factory == FROM_SETTINGS:
        runtime_factory = scripted_runtime if settings.agent_runtime == "scripted" else live_runtime
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.rate_limiter = RateLimiter(settings.api_runs_per_minute)
        app.state.daily_cap = DailyCap(
            settings.live_runs_per_visitor_per_day, settings.live_runs_per_ip_per_day
        )
        app.state.replays = load_replays(settings.replays_dir)
        app.state.engine = db.create_engine(settings.database_url)
        app.state.weaviate = vectorstore.create_client(settings)
        if settings.app_env != "test":
            # Awaited before serving: the client library's connect stalls the event loop
            # (~0.8 s measured locally), which must not hit in-flight requests. Tests
            # skip it; their first real query connects through with_reconnect.
            await _connect_weaviate(app.state.weaviate, settings)
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
        expose_headers=["Retry-After", LIMIT_HEADER],  # the UI words limit messages by these
    )
    app.include_router(health.router)
    app.include_router(threads.router)
    app.include_router(data.router)
    app.include_router(replays_router)
    return app


app = create_app()
