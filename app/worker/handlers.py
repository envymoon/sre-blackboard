"""Job handlers. Each must be idempotent: it may run more than once for the same job."""

from __future__ import annotations

from app.db import incidents

from .runner import Handler, JobContext, PermanentJobError


async def triage(ctx: JobContext) -> None:
    """RECEIVED -> TRIAGED. Investigation (M4) will be enqueued from here."""
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


def default_handlers() -> dict[str, Handler]:
    return {"triage": triage}
