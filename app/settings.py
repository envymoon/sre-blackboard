"""Runtime configuration, read from environment variables (prefix ``SRE_``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SRE_", env_file=".env", extra="ignore")

    # Default model for every agent role; per-role overrides go in llm_role_models.
    llm_model: str = "claude-haiku-5-5"
    llm_role_models: dict[str, str] = {}

    kb_dir: str = "kb"


@lru_cache
def get_settings() -> Settings:
    return Settings()
