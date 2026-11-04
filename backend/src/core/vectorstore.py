"""Weaviate v4 async client factory and health check."""

import weaviate
from weaviate.classes.init import AdditionalConfig, Auth, Timeout

from core.config import Settings


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
