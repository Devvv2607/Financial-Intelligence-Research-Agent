"""Typed application settings loaded from environment variables / `.env`.

Why pydantic-settings: one validated source of truth for config, so a typo in an
env var fails at startup instead of deep inside a request.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"

    # Postgres (psycopg 3 driver for both SQLAlchemy and the LangGraph checkpointer).
    database_url: str = "postgresql+psycopg://fira:fira@localhost:5433/fira"

    # Weaviate (HTTP + gRPC; the v4 client needs both).
    weaviate_http_host: str = "localhost"
    weaviate_http_port: int = 8090
    weaviate_grpc_host: str = "localhost"
    weaviate_grpc_port: int = 50051
    weaviate_api_key: SecretStr | None = None

    # LLM. Model names live only in env, never in code.
    groq_api_key: SecretStr | None = None
    groq_model_reasoning: str = ""
    groq_model_fast: str = ""

    # Data sources.
    sec_user_agent: str = ""
    newsapi_key: SecretStr | None = None
    news_live_fallback: bool = True  # forced off in production (NewsAPI free-tier terms)

    # API.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    readiness_timeout_s: float = 3.0

    @property
    def news_live_fallback_enabled(self) -> bool:
        return self.news_live_fallback and self.app_env != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
