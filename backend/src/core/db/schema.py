"""Relational schema (SQLAlchemy Core).

Why Core instead of the ORM: the pipeline does bulk upserts and the agent does
read-mostly queries; explicit tables + `insert ... on conflict` are clearer than
ORM session state for both, and the same `MetaData` drives Alembic autogenerate.

Natural keys are used wherever SEC provides one (CIK, accession number), which is
what makes ingestion idempotent: re-running an upsert with the same key is a no-op.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    func,
)

# Postgres ARRAY (not the generic one): it provides .overlap()/.contains() for filters.
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _updated_at() -> Column[object]:
    return Column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


companies = Table(
    "companies",
    metadata,
    Column("cik", Integer, primary_key=True, autoincrement=False),  # SEC CIK, a natural key
    Column("ticker", String(16), nullable=False, unique=True),
    Column("name", Text, nullable=False),
    # Hand-picked coverage bucket from data/universe.csv; a reference label only (D-018).
    Column("universe_sector", Text),
    Column("sic", Integer),
    Column("sic_description", Text),
    Column("exchanges", ARRAY(Text)),
    Column("fiscal_year_end", String(4)),  # MMDD, as SEC reports it
    # Two independent sector signals (BUILD_SPEC 3.4) and their disagreement flag.
    Column("sic_sector", Text),
    Column("llm_sector", Text),
    Column("llm_sub_industry", Text),
    Column("llm_sector_confidence", Float),
    Column("llm_sector_rationale", Text),
    Column("sector_conflict", Boolean),
    _updated_at(),
)

filings = Table(
    "filings",
    metadata,
    Column("accession_no", String(25), primary_key=True),
    Column("cik", Integer, ForeignKey("companies.cik", ondelete="CASCADE"), nullable=False),
    Column("form_type", String(16), nullable=False),
    Column("filing_date", Date, nullable=False),
    Column("report_date", Date),
    Column("fiscal_year", Integer),  # 10-K only: year of the period end date
    Column("primary_doc", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("items", ARRAY(Text)),  # 8-K item codes, e.g. {"2.02","9.01"}
    _updated_at(),
    Index("ix_filings_cik_form_date", "cik", "form_type", "filing_date"),
)

filing_sections = Table(
    "filing_sections",
    metadata,
    Column("accession_no", String(25), ForeignKey("filings.accession_no", ondelete="CASCADE")),
    Column("section", String(32), nullable=False),  # item_1 | item_1a | business_overview
    Column("text", Text, nullable=False),
    Column("char_count", Integer, nullable=False),
    Column("method", String(16), nullable=False),  # regex | llm
    _updated_at(),
    PrimaryKeyConstraint("accession_no", "section"),
)

risk_factors = Table(
    "risk_factors",
    metadata,
    Column("id", String(32), primary_key=True),  # content hash: stable across re-runs
    Column("accession_no", String(25), ForeignKey("filings.accession_no", ondelete="CASCADE")),
    Column("cik", Integer, ForeignKey("companies.cik", ondelete="CASCADE"), nullable=False),
    Column("fiscal_year", Integer, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("subsection", Text),
    Column("heading", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("category", String(32)),
    Column("summary", Text),
    Column("tag_source", String(16)),  # llm | inherited
    # Prompt version + model that produced the tag, e.g. risk_tagging:v2@openai/gpt-oss-20b
    Column("tag_version", String(96)),
    Column("inherited_from", String(32)),
    Column("similarity", Float),
    # Tagged "other" because the model never returned a valid category (D-003).
    Column("needs_review", Boolean, nullable=False, server_default="false"),
    _updated_at(),
    Index("ix_risk_factors_cik_year", "cik", "fiscal_year"),
)

eight_k_events = Table(
    "eight_k_events",
    metadata,
    Column(
        "accession_no",
        String(25),
        ForeignKey("filings.accession_no", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("cik", Integer, ForeignKey("companies.cik", ondelete="CASCADE"), nullable=False),
    Column("filing_date", Date, nullable=False),
    Column("items", ARRAY(Text), nullable=False),
    Column("body", Text, nullable=False),
    _updated_at(),
)

financial_facts = Table(
    "financial_facts",
    metadata,
    Column("cik", Integer, ForeignKey("companies.cik", ondelete="CASCADE")),
    Column("metric", String(32), nullable=False),
    Column("fiscal_year", Integer, nullable=False),
    Column("value", Numeric(24, 4), nullable=False),
    Column("unit", String(16), nullable=False),
    Column("concept", Text, nullable=False),  # which us-gaap concept was used (D-012)
    Column("period_start", Date),
    Column("period_end", Date, nullable=False),
    Column("accession_no", String(25), nullable=False),
    Column("filed", Date, nullable=False),
    _updated_at(),
    PrimaryKeyConstraint("cik", "metric", "fiscal_year"),
)

news_articles = Table(
    "news_articles",
    metadata,
    Column("id", String(32), primary_key=True),  # hash of the canonical URL
    Column("cik", Integer, ForeignKey("companies.cik", ondelete="CASCADE"), nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text),
    Column("content_snippet", Text),
    Column("url", Text, nullable=False),
    Column("source_name", Text),
    Column("published_at", DateTime(timezone=True), nullable=False),
    _updated_at(),
    Index("ix_news_cik_published", "cik", "published_at"),
)

ingestion_jobs = Table(
    "ingestion_jobs",
    metadata,
    Column("cik", Integer, ForeignKey("companies.cik", ondelete="CASCADE")),
    Column("stage", String(16), nullable=False),  # edgar | tagging | news | index
    Column("status", String(16), nullable=False),  # running | done | failed
    Column("error", Text),
    Column("details", JSONB),
    Column("run_id", String(96)),  # the ingestion run that last set this status
    Column("attempts", Integer, nullable=False, server_default="0"),
    _updated_at(),
    PrimaryKeyConstraint("cik", "stage"),
)

# Alternative tagging modes measured against production tags (e.g. headings-only, D-003).
risk_tag_experiments = Table(
    "risk_tag_experiments",
    metadata,
    Column("risk_factor_id", String(32), ForeignKey("risk_factors.id", ondelete="CASCADE")),
    Column("mode", String(128), nullable=False),
    Column("category", String(32), nullable=False),
    Column("summary", Text),
    Column("needs_review", Boolean, nullable=False, server_default="false"),
    _updated_at(),
    PrimaryKeyConstraint("risk_factor_id", "mode"),
)

# Every Groq request (and cache hit) — the source for token/cost reports (D-003).
llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("model", Text, nullable=False),
    Column("stage", String(48), nullable=False),
    Column("cik", Integer),
    Column("run_id", String(96)),
    Column("prompt_tokens", Integer, nullable=False, server_default="0"),
    # Groq prompt-cache hits within prompt_tokens; these don't count toward rate limits.
    Column("cached_tokens", Integer, nullable=False, server_default="0"),
    Column("completion_tokens", Integer, nullable=False, server_default="0"),
    Column("reasoning_tokens", Integer),
    Column("latency_ms", Integer, nullable=False, server_default="0"),
    # On a response-cache hit: what the original call cost (lets reports price a full pass).
    Column("saved_tokens", Integer, nullable=False, server_default="0"),
    Column("cache_hit", Boolean, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("error", Text),
    Index("ix_llm_calls_model_created", "model", "created_at"),
)

llm_cache = Table(
    "llm_cache",
    metadata,
    Column("key", String(64), primary_key=True),  # sha256 of model + schema + messages
    Column("model", Text, nullable=False),
    Column("response", JSONB, nullable=False),
    Column("counted_tokens", Integer),  # counted tokens of the call that produced it
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

# Agent run log (written by the agent from Phase 3).
runs = Table(
    "runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("thread_id", Text, nullable=False),
    Column("query", Text, nullable=False),
    Column("route", String(16)),
    Column("answer", Text),
    Column("plan", JSONB),
    Column("steps", JSONB),
    Column("node_usage", JSONB),  # tokens/calls/cache hits per graph node, from llm_calls
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("latency_ms", Integer),
    Column("cost_usd", Numeric(12, 6)),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)
