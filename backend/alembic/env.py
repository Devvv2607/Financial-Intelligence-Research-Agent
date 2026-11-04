"""Alembic environment: async engine on psycopg 3, URL from Settings."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from core import aio
from core.config import get_settings
from core.db.engine import create_engine
from core.db.schema import metadata

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_engine(get_settings().database_url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=get_settings().database_url, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    # core.aio: Windows needs a selector loop for async psycopg (D-016).
    aio.run(_run_async())
