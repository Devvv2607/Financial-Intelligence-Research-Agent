"""Checkpointers with our state types registered for (de)serialization.

LangGraph's msgpack serde warns about, and will soon refuse, deserializing Pydantic types
it hasn't been told about. Registering the exact types the state holds keeps checkpoints
loadable without opening deserialization to arbitrary classes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from core.db.engine import CONNECT_TIMEOUT_S

STATE_TYPES: list[tuple[str, str]] = [
    ("agent.state", name)
    for name in (
        "Entity", "Uncovered", "Clarification", "Analysis", "ToolCallSpec", "PlanStep",
        "ToolCallLog", "StepResult", "Critique", "Citation",
    )
] + [("agent.tools.base", "Evidence")]  # fmt: skip


def serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES)


def memory_saver() -> InMemorySaver:
    return InMemorySaver(serde=serializer())


@asynccontextmanager
async def postgres_saver(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Postgres checkpointer keyed by thread_id; creates its tables on first use."""
    conn_string = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    sep = "&" if "?" in conn_string else "?"
    conn_string += f"{sep}connect_timeout={CONNECT_TIMEOUT_S}"  # fail fast if Postgres is down
    async with AsyncPostgresSaver.from_conn_string(conn_string, serde=serializer()) as saver:
        await saver.setup()
        yield saver
