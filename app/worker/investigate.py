"""The investigate job: TRIAGED -> INVESTIGATING -> DIAGNOSED (and queue the plan job), or ESCALATED.

Like every handler it may run more than once for the same incident. INVESTIGATING is the
resume point: a rerun finds the incident already there and picks up from the agent
checkpoints (app/agents/investigation.py), so finished agents are not asked again.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial

import httpx

from app.agents import variants
from app.agents.investigation import AgentConfig, Outcome, investigate
from app.agents.loop import Deps
from app.agents.store import PgAgentStore
from app.core.config import Settings
from app.db import incidents, investigations
from app.ingest import PRIORITY_NAMES
from app.kb.search import LazyKnowledgeBase
from app.llm.router import build_backend, model_for
from app.llm.types import LLMBackend, LLMError
from app.queue import jobs
from app.tools import ALL_TOOLS, ToolRegistry
from app.tools.clients import Clients
from app.tools.evidence import PgEvidenceStore
from app.tools.sources import LiveSource, Source

from .runner import JobContext, PermanentJobError

log = logging.getLogger(__name__)


@dataclass
class AgentServices:
    """Shared by every job in a worker process: the model backend (its concurrency limit is
    per process), the tool source (its circuit breakers too), Prometheus for verification."""

    backend: LLMBackend
    source: Source
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    # Read by the verify job (app/remediation/verify.py); None: repairs cannot be verified.
    prometheus: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentServices:
        clients = Clients.from_settings(settings)
        # The knowledge base behind kb_search (M6); its models load on the first search.
        kb = LazyKnowledgeBase(settings)
        clients.extra["kb"] = kb
        return cls(build_backend(settings), LiveSource(clients), [clients.aclose, kb.aclose], clients.prometheus)

    async def aclose(self) -> None:
        for close in self.closers:
            await close()


def make_investigate(services: AgentServices) -> Callable[[JobContext], Awaitable[None]]:
    async def investigate_job(ctx: JobContext) -> None:
        if ctx.job.incident_id is None:
            raise PermanentJobError("investigate job without incident_id")
        actor = f"worker:{ctx.worker_id}"
        async with ctx.transaction() as conn:
            incident = await incidents.get(conn, ctx.job.incident_id, tenant_id=None)
            if incident is None:
                raise PermanentJobError(f"incident {ctx.job.incident_id} not found")
            if incident["state"] == "TRIAGED":
                version = await incidents.transition(
                    conn,
                    incident["id"],
                    from_state="TRIAGED",
                    to_state="INVESTIGATING",
                    expected_version=incident["version"],
                    actor=actor,
                    reason="investigation started",
                )
            elif incident["state"] == "INVESTIGATING":
                version = incident["version"]  # a rerun: resume from the checkpoints
            else:
                return  # already diagnosed, escalated or closed
            view = await investigations.incident_view(conn, incident)

        settings = ctx.settings
        deps = Deps(
            backend=services.backend,
            registry=ToolRegistry(ALL_TOOLS, services.source, PgEvidenceStore(ctx.engine)),
            store=PgAgentStore(ctx.engine, fenced=ctx.transaction),
            tenant_id=view.tenant_id,
            incident_id=view.id,
            token_budget=settings.llm_incident_token_budget,
        )
        config = AgentConfig(
            model_for=partial(model_for, settings=settings),
            max_steps=settings.agent_max_steps,
            variant=variants.for_tenant(view.tenant_id, settings.eval_tenant_variants),
        )
        try:
            outcome = await investigate(view, deps, config)
        except LLMError as e:
            if not e.retryable or ctx.job.attempts >= ctx.job.max_attempts:
                outcome = Outcome(escalate=f"the model was unavailable: {e}")
            else:
                raise  # the queue retries the job later; finished agents are kept

        async with ctx.transaction() as conn:
            if outcome.escalate is None and outcome.diagnosis is not None:
                top = outcome.diagnosis.root_causes[0]
                await incidents.transition(
                    conn,
                    view.id,
                    from_state="INVESTIGATING",
                    to_state="DIAGNOSED",
                    expected_version=version,
                    actor=actor,
                    reason=f"root cause candidate: {top.service} ({top.confidence:.0%}): {top.cause}"[:500],
                )
                if settings.auto_remediate:
                    await jobs.enqueue(
                        conn,
                        tenant_id=view.tenant_id,
                        incident_id=view.id,
                        kind="plan",
                        priority=PRIORITY_NAMES.index(view.priority),
                        max_attempts=settings.queue_max_attempts,
                        aging_step_s=settings.queue_aging_step_s,
                        idempotency_key=f"{view.id}:plan",
                    )
            else:
                await incidents.transition(
                    conn,
                    view.id,
                    from_state="INVESTIGATING",
                    to_state="ESCALATED",
                    expected_version=version,
                    actor=actor,
                    reason=(outcome.escalate or "no diagnosis")[:500],
                )
        log.info("incident %s: %s (agents: %s)", view.id, outcome.escalate or "diagnosed", outcome.agents)

    return investigate_job
