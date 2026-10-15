"""Job handlers. Each must be idempotent: it may run more than once for the same job."""

from __future__ import annotations

from app.db import incidents
from app.queue import jobs

from .investigate import AgentServices, make_investigate
from .runner import Handler, JobContext, PermanentJobError


async def triage(ctx: JobContext) -> None:
    """RECEIVED -> TRIAGED, and queue the investigation (same transaction, so never one without the other)."""
    if ctx.job.incident_id is None:
        raise PermanentJobError("triage job without incident_id")
    async with ctx.transaction() as conn:
        incident = await incidents.get(conn, ctx.job.incident_id, tenant_id=None)
        if incident is None:
            raise PermanentJobError(f"incident {ctx.job.incident_id} not found")
        if incident["state"] != "RECEIVED":
            return  # an earlier attempt already did it
        await incidents.transition(
            conn,
            incident["id"],
            from_state="RECEIVED",
            to_state="TRIAGED",
            expected_version=incident["version"],
            actor=f"worker:{ctx.worker_id}",
            reason="accepted for investigation",
        )
        await jobs.enqueue(
            conn,
            tenant_id=incident["tenant_id"],
            incident_id=incident["id"],
            kind="investigate",
            priority=incident["priority"],
            max_attempts=ctx.settings.queue_max_attempts,
            aging_step_s=ctx.settings.queue_aging_step_s,
            idempotency_key=f"{incident['id']}:investigate",
        )


def default_handlers(services: AgentServices) -> dict[str, Handler]:
    return {"triage": triage, "investigate": make_investigate(services)}
