"""Which backend, and which model for each agent role."""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.llm.api import AnthropicAPIBackend
from app.llm.cli import ClaudeCLIBackend
from app.llm.replay import LLMCassette, ReplayBackend
from app.llm.retry import RetryingBackend
from app.llm.types import LLMBackend


def model_for(role: str, settings: Settings) -> str:
    """A role ("coordinator", "investigator.metrics", ...) uses its own model if configured,
    then its family's ("investigator"), then the default."""
    family = role.split(".", 1)[0]
    return settings.llm_role_models.get(role) or settings.llm_role_models.get(family) or settings.llm_model


def build_backend(settings: Settings) -> LLMBackend:
    kind = settings.llm_backend
    if kind == "cli":
        inner: LLMBackend = ClaudeCLIBackend(
            binary=settings.llm_cli_binary,
            timeout_s=settings.llm_timeout_s,
            concurrency=settings.llm_concurrency,
            effort=settings.llm_effort,
        )
    elif kind == "api":
        if settings.anthropic_api_key is None:
            raise ValueError("SRE_LLM_BACKEND=api needs SRE_ANTHROPIC_API_KEY")
        inner = AnthropicAPIBackend(
            settings.anthropic_api_key.get_secret_value(),
            base_url=settings.anthropic_base_url,
            timeout_s=settings.llm_timeout_s,
        )
    elif kind == "replay":
        if not settings.llm_cassette:
            raise ValueError("SRE_LLM_BACKEND=replay needs SRE_LLM_CASSETTE")
        return ReplayBackend(LLMCassette.load(Path(settings.llm_cassette)))
    else:
        raise ValueError(f"unknown SRE_LLM_BACKEND {kind!r} (cli, api or replay)")
    return RetryingBackend(inner, retries=settings.llm_retries)
