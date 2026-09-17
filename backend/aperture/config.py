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
    # owner connection, only used after an explicit human approval of a write
    owner_database_url: str | None = None

    # bedrock
    bedrock_model_id: str = "qwen.qwen3-coder-next"
    bedrock_region: str = "us-east-1"
    max_tokens: int = 800
    temperature: float = 0.0

    # guards
    max_repair_attempts: int = 2
    row_limit: int = 1000
    statement_timeout_ms: int = 30_000
    # reject plans whose estimated total cost exceeds this (planner cost units)
    max_estimated_cost: float = 5_000_000

    # budget ledger, USD
    budget_ceiling_usd: float = 10.0
    budget_warn_usd: float = 5.0
    # Bedrock does not expose per-model pricing through the runtime API, so
    # these are configured rather than discovered. Defaults are deliberately
    # pessimistic: overestimating price makes the ceiling trip early, which is
    # the safe direction to be wrong in.
    price_in_per_mtok: float = 0.90
    price_out_per_mtok: float = 2.70

    # embeddings
    embed_model: str = "BAAI/bge-base-en-v1.5"

    # where Aperture keeps its own state: checkpoints, schema cache, ledger
    home_dir: str = "~/.aperture"


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
