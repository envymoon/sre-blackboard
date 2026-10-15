"""The worker's remediation jobs (ADR-0007): plan a repair, and give up on an unanswered approval.

The worker proposes and reviews; it never touches the cluster. ``execute`` jobs belong to the
executor process (app/executor), which alone holds write credentials.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from app.agents.loop import Deps
from app.agents.store import PgAgentStore
from app.db import incidents, investigations
from app.llm.router import model_for
from app.llm.types import LLMError
from app.remediation import flow, planner
from app.tools import ALL_TOOLS, ToolRegistry
from app.tools.evidence import PgEvidenceStore

from .investigate import AgentServices
from .runner import JobContext, PermanentJobError

log = logging.getLogger(__name__)


def make_plan(services: AgentServices) -> Callable[[JobContext], Awaitable[None]]:
    async def plan_job(ctx: JobContext) -> None:
        """DIAGNOSED -> (planner) -> PLANNED -> SAFETY_REVIEWED -> EXECUTING / AWAITING_APPROVAL / ESCALATED.

        The model is asked outside any transaction; everything after it commits at once, so a
        rerun finds the incident either still DIAGNOSED (and reuses the planner's checkpoint)
        or already past it.
        """
        incident_id = _incident_id(ctx)
        async with ctx.engine.connect() as conn:
            incident = await incidents.get(conn, incident_id, tenant_id=None)
            if incident is None:
                raise PermanentJobError(f"incident {incident_id} not found")
            if incident["state"] != "DIAGNOSED":
                return
            view = await investigations.incident_view(conn, incident)
            dx = await flow.diagnosis(conn, incident_id)

        proposal = planner.Proposal(None, "there is no diagnosis to plan from")
        if dx is not None:
            settings = ctx.settings
            deps = Deps(
                backend=services.backend,
                registry=ToolRegistry(ALL_TOOLS, services.source, PgEvidenceStore(ctx.engine)),
                store=PgAgentStore(ctx.engine, fenced=ctx.transaction),
                tenant_id=view.tenant_id,
                incident_id=view.id,
                token_budget=settings.llm_incident_token_budget,
            )
            try:
                proposal = await planner.propose(
                    view, dx, deps, model=model_for(planner.PLANNER, settings), max_steps=settings.planner_max_steps
                )
            except LLMError as e:
                if e.retryable and ctx.job.attempts < ctx.job.max_attempts:
                    raise
                proposal = planner.Proposal(None, f"the model was unavailable: {e}")

        async with ctx.transaction() as conn:
            incident = await incidents.get(conn, incident_id, tenant_id=None)
            if incident is None or incident["state"] != "DIAGNOSED":
                return
            if proposal.plan is None:
                await incidents.transition(
                    conn,
                    incident_id,
                    from_state="DIAGNOSED",
                    to_state="ESCALATED",
                    expected_version=incident["version"],
                    actor=flow.PLANNER_ACTOR,
                    reason=f"no repair plan: {proposal.error}"[:500],
                )
                return
            plan, state = await flow.submit(
                conn,
                incident,
                proposal.plan,
                proposed_by=flow.PLANNER_ACTOR,
                actor=flow.PLANNER_ACTOR,
                settings=ctx.settings,
            )
        log.info("incident %s: plan %s is %s, incident %s", incident_id, plan.plan_hash[:12], plan.verdict, state)

    return plan_job


async def approval_timeout(ctx: JobContext) -> None:
    """A plan nobody decided on in time: the incident goes to a human (ESCALATED)."""
    incident_id = _incident_id(ctx)
    plan_id = ctx.job.payload.get("plan_id")
    async with ctx.transaction() as conn:
        incident = await incidents.get(conn, incident_id, tenant_id=None)
        if incident is None or incident["state"] != "AWAITING_APPROVAL":
            return
        plan = await flow.current_plan(conn, incident_id)
        if plan is None or plan.id != plan_id or await flow.approval(conn, plan.id) is not None:
            return  # revised or decided since
        await incidents.transition(
            conn,
            incident_id,
            from_state="AWAITING_APPROVAL",
            to_state="ESCALATED",
            expected_version=incident["version"],
            actor="policy",
            reason=f"plan {plan.plan_hash[:12]} was not approved within "
            f"{ctx.settings.remediation_approval_ttl_s / 60:.0f} minutes",
        )


def _incident_id(ctx: JobContext) -> UUID:
    if ctx.job.incident_id is None:
        raise PermanentJobError(f"{ctx.job.kind} job without incident_id")
    return ctx.job.incident_id
