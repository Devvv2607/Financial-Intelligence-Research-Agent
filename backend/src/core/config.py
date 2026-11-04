"""Typed application settings loaded from environment variables / `.env`.

Why pydantic-settings: one validated source of truth for config, so a typo in an
env var fails at startup instead of deep inside a request.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelSlot = Literal["reasoning", "fast"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    data_dir: Path = Path("data")

    # Postgres (psycopg 3 driver for both SQLAlchemy and the LangGraph checkpointer).
    database_url: str = "postgresql+psycopg://fira:fira@localhost:5442/fira"

    # Weaviate (HTTP + gRPC; the v4 client needs both).
    weaviate_http_host: str = "localhost"
    weaviate_http_port: int = 8095
    weaviate_grpc_host: str = "localhost"
    weaviate_grpc_port: int = 50055
    weaviate_api_key: SecretStr | None = None

    # LLM. Model names live only in env, never in code.
    groq_api_key: SecretStr | None = None
    groq_model_reasoning: str = ""
    groq_model_fast: str = ""
    # Models for the agent and dev testing. Set these to a different model than ingestion
    # uses so both don't draw on the same per-model daily quota (empty = same as above).
    agent_model_reasoning: str = ""
    agent_model_fast: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_s: float = 90.0
    # Per-model account limits (free-tier defaults). Used for pacing and run projections.
    groq_tokens_per_minute: int = 8_000
    groq_requests_per_minute: int = 30
    groq_tokens_per_day: int = 200_000
    groq_requests_per_day: int = 1_000
    # Our own stop line per model per rolling 24h, below Groq's limit to leave headroom
    # (counted tokens only: Groq doesn't count prompt-cache hits toward limits).
    groq_daily_token_budget: int = 170_000
    # The agent model's own stop line (owner: hard 150K/day on gpt-oss-120b, D-027).
    agent_daily_token_budget: int = 150_000
    # Reuse identical agent LLM responses (model + prompt key) during development so
    # re-running a question costs nothing. Off in production.
    agent_dev_cache: bool = True
    # USD per 1M tokens: {"model": [input, output]}. Checked against Groq's model pages.
    groq_prices_usd_per_mtok: dict[str, tuple[float, float]] = Field(default_factory=dict)

    # Embeddings / chunking (D-001, D-009).
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    chunk_tokens: int = 450
    chunk_overlap_tokens: int = 60

    # Data sources.
    sec_user_agent: str = ""
    sec_requests_per_second: float = 8.0
    newsapi_key: SecretStr | None = None
    newsapi_base_url: str = "https://newsapi.org/v2"
    news_window_days: int = 29
    newsapi_requests_per_day: int = 90  # free plan allows 100/day; keep headroom
    news_live_fallback: bool = True  # forced off in production (NewsAPI free-tier terms)

    # Risk tagging (D-003).
    risk_tag_body_words: int = 150
    risk_tag_batch_size: int = 12
    risk_tag_inherit_similarity: float = 0.90
    risk_tag_response_mode: Literal["json_schema", "json_object"] = "json_schema"

    # API.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    readiness_timeout_s: float = 3.0

    @property
    def news_live_fallback_enabled(self) -> bool:
        return self.news_live_fallback and self.app_env != "production"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    def model_for(self, slot: ModelSlot, *, agent: bool = False) -> str:
        model = self.groq_model_reasoning if slot == "reasoning" else self.groq_model_fast
        if agent:
            model = (
                self.agent_model_reasoning if slot == "reasoning" else self.agent_model_fast
            ) or model
        if not model:
            raise ValueError(f"GROQ_MODEL_{slot.upper()} is not set")
        return model


@lru_cache
def get_settings() -> Settings:
    return Settings()
