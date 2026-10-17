"""The execute job, run only by the executor process (ADR-0007).

It trusts nothing the worker decided. Before touching the cluster it checks again that the
incident is EXECUTING with this plan, re-runs the policy (the change window may have closed,
the configuration may have changed), and, for a plan that needs one, that an approval exists
for this exact plan hash from someone other than its author.

Each action runs at most once. Its ``executions`` row (unique per plan and action) is
committed before the real patch is sent. A rerun that finds the row still ``started``
cannot know whether the patch landed, so it marks it ``unknown`` and escalates instead of
sending it again: a rollback applied twice would bring the bad release back.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import audit, incidents
from app.executor.k8s import DeploymentWriter, ExecError, Prepared
from app.queue import jobs
from app.remediation import flow
from app.remediation.actions import CATALOGUE
from app.worker.runner import JobContext, PermanentJobError

log = logging.getLogger(__name__)


def make_execute(writer: DeploymentWriter) -> Callable[[JobContext], Awaitable[None]]:
    async def execute_job(ctx: JobContext) -> None:
        if ctx.job.incident_id is None:
            raise PermanentJobError("execute job without incident_id")
        incident_id: UUID = ctx.job.incident_id
        me = f"executor:{ctx.worker_id}"
        async with ctx.engine.connect() as conn:
            incident = await incidents.get(conn, incident_id, tenant_id=None)
            if incident is None:
                raise PermanentJobError(f"incident {incident_id} not found")
            if incident["state"] != "EXECUTING":
                return
            plan = await flow.current_plan(conn, incident_id)
            refusal = await _refusal(conn, incident, plan, ctx)
        if plan is None or refusal:
            await _escalate(ctx, incident_id, me, f"execution refused: {refusal or 'no plan'}", plan_id=None)
            return

        note = f"sre-blackboard: plan {plan.plan_hash[:12]} for incident {incident_id}"
        for i, action in enumerate(plan.plan.actions):
            key = f"plan:{plan.id}:action:{i}"
            async with ctx.engine.connect() as conn:
                done = (
                    await conn.execute(text("SELECT status FROM executions WHERE idempotency_key = :k"), {"k": key})
                ).scalar_one_or_none()
            if done == "succeeded":
                continue  # an earlier attempt finished this action
            if done is not None:
                if done == "started":
                    async with ctx.transaction() as conn:
                        await conn.execute(
                            text(
                                "UPDATE executions SET status = 'unknown', finished_at = now(), "
                                "error = 'the executor stopped while the change was being sent' WHERE idempotency_key = :k"
                            ),
                            {"k": key},
                        )
                reason = f"{action.action} {action.deployment}: outcome unknown after an interrupted attempt; check the cluster"
                await _escalate(ctx, incident_id, me, reason, plan_id=plan.id)
                return

            base = {
                "tenant": incident["tenant_id"],
                "incident": incident_id,
                "plan": plan.id,
                "index": i,
                "action": action.model_dump_json(),
                "key": key,
                "executor": me,
            }
            # 1. Work out the change and let the API server check it, without applying it.
            try:
                prepared = await writer.prepare(action, note=note)
                dry = await writer.patch(prepared.deployment, prepared.ops, dry_run=True) if prepared.ops else None
            except ExecError as e:
                await _record(ctx, base, "failed", error=str(e))
                await _escalate(
                    ctx, incident_id, me, f"{action.action} {action.deployment} failed before any change: {e}", plan.id
                )
                return
            dry_summary = _dry_summary(dry)

            # 2. Commit the intent, then send the change. From here on a crash means "unknown".
            await _record(ctx, base, "started", dry_run=dry_summary, before=prepared.before)
            async with ctx.transaction() as conn:
                await audit.append(
                    conn,
                    actor=me,
                    event="execution.started",
                    subject=str(incident_id),
                    tenant_id=incident["tenant_id"],
                    payload={
                        "plan_id": plan.id,
                        "action": i,
                        "summary": prepared.summary,
                        "dry_run": dry_summary,
                        "before": prepared.before,
                    },
                )
            try:
                after = await _apply(writer, prepared, ctx.settings.executor_rollout_timeout_s)
            except ExecError as e:
                status = "unknown" if e.applied is None else "failed"
                await _finish(ctx, key, status, error=str(e))
                await _escalate(ctx, incident_id, me, f"{prepared.summary}: {status}: {e}", plan.id)
                return
            await _finish(ctx, key, "succeeded", after=after)
            async with ctx.transaction() as conn:
                await audit.append(
                    conn,
                    actor=me,
                    event="execution.succeeded",
                    subject=str(incident_id),
                    tenant_id=incident["tenant_id"],
                    payload={"plan_id": plan.id, "action": i, "summary": prepared.summary, "after": after},
                )
            log.info("incident %s: %s", incident_id, prepared.summary)

        async with ctx.transaction() as conn:
            current = await incidents.get(conn, incident_id, tenant_id=None)
            if current is None or current["state"] != "EXECUTING":
                return
            await incidents.transition(
                conn,
                incident_id,
                from_state="EXECUTING",
                to_state="VERIFYING",
                expected_version=current["version"],
                actor=me,
                reason=f"plan {plan.plan_hash[:12]} executed; checking the SLO",
            )
            await jobs.enqueue(
                conn,
                tenant_id=current["tenant_id"],
                incident_id=incident_id,
                kind="verify",
                payload={"plan_id": plan.id},
                priority=current["priority"],
                max_attempts=ctx.settings.queue_max_attempts,
                aging_step_s=ctx.settings.queue_aging_step_s,
                idempotency_key=f"plan:{plan.id}:verify",
                delay_s=ctx.settings.verify_settle_s,
            )

    return execute_job


async def _refusal(conn: AsyncConnection, incident: Any, plan: flow.PlanRow | None, ctx: JobContext) -> str:
    """Why this plan must not run now, or "" if it may."""
    if plan is None:
        return "there is no plan"
    if ctx.job.payload.get("plan_id") != plan.id:
        return f"the job is for plan {ctx.job.payload.get('plan_id')}, the current plan is {plan.id}"
    if plan.verdict == "deny":
        return "the policy denied this plan"
    now = await flow.review(conn, incident, plan.plan, ctx.settings)
    if now.verdict == "deny":
        return f"the policy denies it now: {now.reasons()}"
    if plan.verdict == "require_approval" or now.verdict == "require_approval":
        decided = await flow.approval(conn, plan.id)
        if decided is None or decided.decision != "approve":
            return "it needs an approval and has none"
        if decided.plan_hash != plan.plan_hash:
            return "the approval is for a different plan"
        if decided.approver == plan.proposed_by:
            return "it was approved by its own author"
    return ""


async def _apply(writer: DeploymentWriter, prepared: Prepared, rollout_timeout_s: float) -> dict[str, Any]:
    if prepared.noop:
        return {"noop": True, "summary": prepared.summary}
    result = await writer.patch(prepared.deployment, prepared.ops, dry_run=False)
    try:
        rollout = await writer.wait_rolled_out(
            prepared.deployment, int(result["metadata"]["generation"]), rollout_timeout_s
        )
    except ExecError as e:
        # Applied (the API said so); only watching the rollout failed. Verification decides.
        rollout = {"rolled_out": None, "error": str(e)}
    return {"generation": result["metadata"].get("generation"), **rollout}


def _dry_summary(dry: dict[str, Any] | None) -> dict[str, Any] | None:
    if dry is None:
        return None
    return {"accepted": True, "generation": dry.get("metadata", {}).get("generation")}


async def _record(ctx: JobContext, base: dict[str, Any], status: str, **cols: Any) -> None:
    async with ctx.transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO executions (tenant_id, incident_id, plan_id, action_index, action, idempotency_key,
                                        status, executor, dry_run, before, error, finished_at)
                VALUES (:tenant, :incident, :plan, :index, CAST(:action AS jsonb), :key, :status, :executor,
                        CAST(:dry_run AS jsonb), CAST(:before AS jsonb), :error,
                        CASE WHEN :status = 'started' THEN NULL ELSE now() END)
                """
            ),
            {
                **base,
                "status": status,
                "dry_run": json.dumps(cols.get("dry_run")),
                "before": json.dumps(cols.get("before")),
                "error": cols.get("error"),
            },
        )


async def _finish(ctx: JobContext, key: str, status: str, *, after: Any = None, error: str | None = None) -> None:
    async with ctx.transaction() as conn:
        await conn.execute(
            text(
                "UPDATE executions SET status = :status, after = CAST(:after AS jsonb), error = :error, "
                "finished_at = now() WHERE idempotency_key = :k"
            ),
            {"status": status, "after": json.dumps(after), "error": error, "k": key},
        )


async def _escalate(ctx: JobContext, incident_id: UUID, actor: str, reason: str, plan_id: int | None) -> None:
    async with ctx.transaction() as conn:
        current = await incidents.get(conn, incident_id, tenant_id=None)
        if current is None or current["state"] in incidents.TERMINAL:
            return
        if plan_id is not None:
            plan = await flow.current_plan(conn, incident_id)
            undo = (
                "; ".join(f"to undo {a.action}: {CATALOGUE[a.action].inverse}" for a in plan.plan.actions)
                if plan
                else ""
            )
            reason = f"{reason}. {undo}" if undo else reason
        await incidents.transition(
            conn,
            incident_id,
            from_state=current["state"],
            to_state="ESCALATED",
            expected_version=current["version"],
            actor=actor,
            reason=reason[:500],
        )
