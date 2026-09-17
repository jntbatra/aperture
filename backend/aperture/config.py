"""Settings for Aperture, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="APERTURE_", extra="ignore"
    )

    # database under analysis; should point at a read-only role
    database_url: str = "postgresql+psycopg://aperture_ro:aperture_ro_pw@localhost:5433/tiffinwala"
    # Owner connection. Left unset on purpose: an owner credential sitting next
    # to an LLM loop is a liability, and writes are refused rather than escalated.
    owner_database_url: str | None = None

    # bedrock
    bedrock_model_id: str = "qwen.qwen3-coder-next"
    bedrock_region: str = "us-east-1"
    # Generous, because truncated SQL presents as an unfixable parse error and
    # burns a repair attempt that cannot succeed.
    max_tokens: int = 3000
    temperature: float = 0.0
    # Used when a repair attempt returns byte-identical SQL: same prompt at
    # temperature 0 gives the same tokens, so something has to change.
    repair_temperature: float = 0.3
    bedrock_max_retries: int = 8
    bedrock_read_timeout: int = 120

    # guards
    max_repair_attempts: int = 2
    max_empty_retries: int = 1
    row_limit: int = 1000
    statement_timeout_ms: int = 30_000
    # reject plans whose estimated total cost exceeds this (planner cost units)
    max_estimated_cost: float = 5_000_000
    # wall-clock ceiling for one question, across every node and retry
    run_deadline_seconds: int = 120
    # token ceiling for one question, so a pathological retry loop cannot eat
    # a meaningful slice of the global budget
    max_question_tokens: int = 40_000

    # budget ledger, USD
    budget_ceiling_usd: float = 10.0
    budget_warn_usd: float = 5.0
    # Bedrock does not expose per-model pricing through the runtime API, so
    # these are configured rather than discovered. Defaults are deliberately
    # pessimistic: overestimating price makes the ceiling trip early, which is
    # the safe direction to be wrong in.
    price_in_per_mtok: float = 0.90
    price_out_per_mtok: float = 2.70

    # embeddings / vector index
    # all-mpnet-base-v2 is 768-dim and already present in the local HF cache,
    # so first run costs no download.
    embed_model: str = "sentence-transformers/all-mpnet-base-v2"
    # auto | numpy | qdrant -- auto picks numpy below `vector_backend_threshold`
    vector_backend: str = "auto"
    vector_backend_threshold: int = 50_000
    qdrant_url: str = "http://localhost:6333"

    # schema linking
    link_top_k_tables: int = 8
    link_hops: int = 1
    link_table_budget: int = 25

    # Columns whose observed values must never reach a prompt, a trace, or a
    # rendered table. Matched case-insensitively against the column name.
    pii_column_pattern: str = (
        r"email|phone|mobile|address|ssn|pan|aadhaar|passport|password|pin|token|card|cvv|secret"
    )

    # where Aperture keeps its own state: checkpoints, schema cache, ledger
    home_dir: str = "~/.aperture"


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
