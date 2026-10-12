"""Runtime configuration, read from environment variables (prefix ``SRE_``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SRE_", env_file=".env", extra="ignore")

    # Default model for every agent role; per-role overrides go in llm_role_models.
    llm_model: str = "claude-haiku-5-5"
    llm_role_models: dict[str, str] = {}

    kb_dir: str = "kb"

    database_url: str = "postgresql+psycopg://sre:sre@localhost:5432/sre"
    db_pool_size: int = 10

    # HS256 signing key for access tokens. No default: the API refuses to start without one.
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "sre-blackboard"
    jwt_audience: str = "sre-blackboard"
    # Browser origins allowed to call the API. Empty means no CORS headers at all.
    cors_origins: list[str] = []

    # Job queue. A lease is renewed every lease_ttl_s / 3 seconds while a job runs.
    queue_lease_ttl_s: float = 30.0
    # Each priority level below P0 pushes a job back by this much in the claim order,
    # so a P3 job is served before any P0 that arrives more than 3 steps after it.
    queue_aging_step_s: float = 60.0
    queue_max_attempts: int = 5
    queue_backoff_base_s: float = 2.0
    queue_backoff_max_s: float = 300.0

    worker_concurrency: int = 4
    # Slots that only P0 jobs may take, so a P0 never waits behind a full worker.
    worker_reserved_p0_slots: int = 1
    worker_poll_interval_s: float = 0.5
    worker_reap_interval_s: float = 5.0
    worker_shutdown_grace_s: float = 20.0
    # A handler running longer than this fails the attempt (and is retried).
    worker_job_timeout_s: float = 600.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
