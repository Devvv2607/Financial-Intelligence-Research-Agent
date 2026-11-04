"""Weaviate v4 async client factory, health check, and collection schemas."""

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.init import AdditionalConfig, Auth, Timeout

from core.config import Settings

FILING_CHUNK = "FilingChunk"
RISK_FACTOR = "RiskFactor"
NEWS_ARTICLE = "NewsArticle"


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
