"""Runtime configuration, read from environment variables (prefix ``SRE_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # .lab.env holds the lab's tool endpoints and a short-lived Kubernetes token
    # (`make lab-env`); values there override .env.
    model_config = SettingsConfigDict(env_prefix="SRE_", env_file=(".env", ".lab.env"), extra="ignore")

    # Default model for every agent role. Overrides by role ("investigator.metrics") or by
    # family ("investigator"), e.g. SRE_LLM_ROLE_MODELS='{"synthesizer": "claude-sonnet-5-5"}'.
    llm_model: str = "claude-haiku-5-5"
    llm_role_models: dict[str, str] = {}
    # "cli" (claude -p, subscription), "api" (Anthropic API key) or "replay" (llm_cassette).
    llm_backend: str = "cli"
    llm_cli_binary: str = "claude"
    llm_cassette: str | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    llm_timeout_s: float = 180.0
    llm_retries: int = 3
    # claude -p processes at once, per worker process.
    llm_concurrency: int = 2
    # Tokens (prompt, cached or not, plus output) one incident may use before it is escalated.
    llm_incident_token_budget: int = 1_500_000
    agent_max_steps: int = 8
    # Queue an investigation for every new incident. Off: triage only (no model access, or
    # a load test of the ingest path).
    auto_investigate: bool = True

    kb_dir: str = "kb"

    # Read-only tools (M3). Defaults are the lab's ports on the host (deploy/kind/cluster.yaml).
    prometheus_url: str = "http://localhost:19090"
    loki_url: str = "http://localhost:13100"
    jaeger_url: str = "http://localhost:18080/jaeger/ui"
    flagd_ui_url: str = "http://localhost:18080/feature"
    # The Kubernetes API, read with a ServiceAccount token that can only get/list in one namespace.
    k8s_api_url: str = "https://127.0.0.1:6443"
    k8s_token: SecretStr | None = None
    k8s_ca_file: str | None = None
    k8s_namespace: str = "otel-demo"

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
    # An investigation (about 20 model steps, a few in parallel) is the longest job.
    worker_job_timeout_s: float = 1200.0

    # Remediation (M5, ADR-0007). After a diagnosis, plan a repair and put it to the policy.
    auto_remediate: bool = True
    planner_max_steps: int = 3
    # Deployments in k8s_namespace that remediation may touch. Empty: none (fail closed).
    remediation_targets: list[str] = []
    # The top root cause must be at least this sure before anything is done about it.
    remediation_min_confidence: float = 0.5
    # Plans up to this risk run without a human; above it they wait for an approval.
    # R1 means: every action in the catalogue (R2 and R3) needs one.
    remediation_auto_max_risk: Literal["R0", "R1", "R2", "R3"] = "R1"
    # When changes may be made, UTC, "HH:MM-HH:MM" (may wrap past midnight).
    remediation_change_window: str = "00:00-24:00"
    # How long a plan waits for an approval before the incident is escalated.
    remediation_approval_ttl_s: float = 1800.0

    # The executor's own Kubernetes identity: patch Deployments in k8s_namespace, nothing else.
    # Only the executor process is given it; workers never are.
    executor_k8s_token: SecretStr | None = None
    # How long the executor watches a rollout before handing over to verification anyway.
    executor_rollout_timeout_s: float = 180.0

    # Verification after a repair: wait, then check the SLI every interval until it has been
    # healthy for verify_healthy_checks checks in a row, or verify_timeout_s has passed.
    verify_settle_s: float = 60.0
    verify_interval_s: float = 30.0
    verify_healthy_checks: int = 3
    verify_timeout_s: float = 600.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
