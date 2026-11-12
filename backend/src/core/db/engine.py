"""Async SQLAlchemy engine on the psycopg 3 driver.

Why psycopg 3 (not asyncpg): LangGraph's PostgresSaver requires psycopg 3, so using
it for SQLAlchemy too means one driver, one connection-string format, one set of quirks.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

CONNECT_TIMEOUT_S = 10


def create_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping: drop dead connections (e.g. after a Postgres restart) instead of erroring.
    # connect_timeout: fail fast when Postgres is down (without it the CLI hung for minutes).
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )


async def ping(engine: AsyncEngine) -> None:
    """Raise if Postgres is unreachable."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
