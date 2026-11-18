"""Weaviate v4 async client factory, connection lifecycle, health check, and schemas.

Lifecycle (D-053): one client per process, created and connected in the API lifespan.
Every query goes through `with_reconnect`, which connects a client that isn't connected
and, on a closed client or a dropped connection, reconnects once and retries once. Before
this, nothing connected the API's client except `/ready`, so after every deploy all
Weaviate queries failed with WeaviateClosedClientError until someone opened `/ready`.
"""

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.init import AdditionalConfig, Auth, Timeout
from weaviate.exceptions import (
    WeaviateClosedClientError,
    WeaviateConnectionError,
    WeaviateGRPCUnavailableError,
)

from core.config import Settings

FILING_CHUNK = "FilingChunk"
RISK_FACTOR = "RiskFactor"
NEWS_ARTICLE = "NewsArticle"

logger = logging.getLogger(__name__)
Client = weaviate.WeaviateAsyncClient
# A closed or never-connected client, or a connection that dropped: worth one reconnect.
RECONNECTABLE: tuple[type[Exception], ...] = (
    WeaviateClosedClientError,
    WeaviateConnectionError,
    WeaviateGRPCUnavailableError,
)
# One lock per client, so concurrent requests that all hit a dropped connection
# reconnect it once instead of racing close()/connect() on the same client.
_locks: "weakref.WeakKeyDictionary[Client, asyncio.Lock]" = weakref.WeakKeyDictionary()
# Bumped by each reconnect: a request that saw the old connection fail doesn't reconnect
# again when another request already replaced it.
_generation: "weakref.WeakKeyDictionary[Client, int]" = weakref.WeakKeyDictionary()


def create_client(settings: Settings) -> weaviate.WeaviateAsyncClient:
    """Build an unconnected client. Call `ensure_ready` (or `connect`) before use.

    Not connecting here lets the API boot even when Weaviate is down; `/ready`
    then reports the outage instead of the process crash-looping.
    """
    auth = (
        Auth.api_key(settings.weaviate_api_key.get_secret_value())
        if settings.weaviate_api_key
        else None
    )
    return weaviate.use_async_with_custom(
        http_host=settings.weaviate_http_host,
        http_port=settings.weaviate_http_port,
        http_secure=settings.weaviate_http_port == 443,
        grpc_host=settings.weaviate_grpc_host,
        grpc_port=settings.weaviate_grpc_port,
        grpc_secure=settings.weaviate_grpc_port == 443,
        auth_credentials=auth,
        additional_config=AdditionalConfig(timeout=Timeout(init=5, query=30, insert=120)),
    )


async def ensure_ready(client: weaviate.WeaviateAsyncClient) -> bool:
    """Connect lazily (HTTP + gRPC handshake), then ask Weaviate if it is ready."""
    if not client.is_connected():
        await client.connect()
    return await client.is_ready()


def _lock(client: Client) -> asyncio.Lock:
    lock = _locks.get(client)
    if lock is None:
        lock = _locks[client] = asyncio.Lock()
    return lock


async def connect(client: Client) -> None:
    """Connect if not connected (idempotent; safe to call from many requests)."""
    if client.is_connected():
        return
    async with _lock(client):
        if not client.is_connected():
            await client.connect()


async def reconnect(client: Client, broken_since: int) -> None:
    """Close and connect again, unless another request already did since `broken_since`."""
    async with _lock(client):
        if _generation.get(client, 0) != broken_since:
            return  # someone else reconnected while we waited for the lock
        try:
            await client.close()
        except Exception:  # closing a broken connection may fail; we replace it anyway
            logger.debug("weaviate close before reconnect failed", exc_info=True)
        await client.connect()
        _generation[client] = broken_since + 1


async def with_reconnect[T](client: Client, op: Callable[[Client], Awaitable[T]]) -> T:
    """Run one Weaviate operation. Connects a client that isn't connected; on a closed
    client or a dropped connection, reconnects once and retries the operation once.
    Operations must be safe to repeat (queries; inserts with deterministic uuids)."""
    generation = _generation.get(client, 0)
    try:
        await connect(client)
        return await op(client)
    except RECONNECTABLE as exc:
        logger.warning("weaviate reconnect", extra={"error": type(exc).__name__})
        await reconnect(client, generation)
        return await op(client)


async def count_objects(client: Client, collection: str) -> int:
    """A real query (aggregate over gRPC, like the search tools use): the readiness probe."""
    res = await with_reconnect(
        client, lambda c: c.collections.get(collection).aggregate.over_all(total_count=True)
    )
    if res.total_count is None:
        raise RuntimeError(f"{collection}: Weaviate returned no count")
    return int(res.total_count)


def _text(name: str) -> Property:
    return Property(name=name, data_type=DataType.TEXT)


def _keyword(name: str) -> Property:
    # Exact-match filter fields: no tokenization, excluded from BM25.
    return Property(
        name=name,
        data_type=DataType.TEXT,
        tokenization=weaviate.classes.config.Tokenization.FIELD,
        index_searchable=False,
    )


# Shared filterable metadata (BUILD_SPEC 3.5). `text`-typed fields feed BM25.
_COMMON = [
    _keyword("ticker"),
    Property(name="cik", data_type=DataType.INT),
    _keyword("company"),
    _keyword("sector"),
    _keyword("source_url"),
]

COLLECTIONS: dict[str, list[Property]] = {
    FILING_CHUNK: [
        _text("text"),
        *_COMMON,
        _keyword("form_type"),
        Property(name="fiscal_year", data_type=DataType.INT),
        Property(name="filing_date", data_type=DataType.DATE),
        _keyword("section"),
        Property(name="item_codes", data_type=DataType.TEXT_ARRAY, index_searchable=False),
        _keyword("accession_no"),
        Property(name="chunk_index", data_type=DataType.INT),
    ],
    RISK_FACTOR: [
        _text("heading"),
        _text("body"),
        _text("summary"),
        *_COMMON,
        _keyword("risk_category"),
        Property(name="fiscal_year", data_type=DataType.INT),
        Property(name="filing_date", data_type=DataType.DATE),
        _keyword("form_type"),
        _keyword("accession_no"),
        _keyword("risk_id"),
    ],
    NEWS_ARTICLE: [
        _text("title"),
        _text("description"),
        _text("content"),
        *_COMMON,
        _keyword("source_name"),
        Property(name="published_at", data_type=DataType.DATE),
        _keyword("article_id"),
    ],
}


async def ensure_collections(client: weaviate.WeaviateAsyncClient) -> None:
    """Create missing collections. Vectors are bring-your-own (fastembed, D-009)."""
    for name, props in COLLECTIONS.items():
        if await client.collections.exists(name):
            continue
        await client.collections.create(
            name,
            properties=props,
            vector_config=Configure.Vectors.self_provided(),
        )
