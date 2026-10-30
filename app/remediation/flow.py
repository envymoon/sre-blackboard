"""The remediation part of the state machine, in the database (ADR-0007).

    DIAGNOSED -> PLANNED -> SAFETY_REVIEWED -> EXECUTING                 (policy: allow)
                                            -> AWAITING_APPROVAL         (policy: require_approval)
                                            -> ESCALATED                 (policy: deny, or no actions)
    AWAITING_APPROVAL -> EXECUTING (approved) | ESCALATED (rejected, timed out) | PLANNED (revised)

Every function takes the caller's connection, so a plan, its verdict, the state moves, the
audit rows and the follow-up job commit together or not at all.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agents.schemas import Diagnosis
from app.core.config import Settings
from app.db import audit, incidents
from app.obs import metrics, tracing
from app.queue import jobs
from app.remediation.plans import RemediationPlan, describe, plan_hash
from app.remediation.policy import Decision, EvidenceRef, PolicyContext, evaluate

PLANNER_ACTOR = "agent:planner"


@dataclass(frozen=True)
class PlanRow:
    id: int
    tenant_id: str
    incident_id: UUID
    seq: int
    proposed_by: str
    plan: RemediationPlan
    plan_hash: str
    verdict: str
    risk: str | None
    checks: list[dict[str, Any]]
    expires_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> PlanRow:
        m = dict(row._mapping)
        return cls(**{**m, "plan": RemediationPlan.model_validate(m["plan"])})


async def diagnosis(conn: AsyncConnection, incident_id: UUID) -> Diagnosis | None:
    body = await conn.scalar(
        text("SELECT body FROM blackboard WHERE incident_id = :id AND kind = 'diagnosis' ORDER BY id DESC LIMIT 1"),
        {"id": incident_id},
    )
    return Diagnosis.model_validate(body) if body is not None else None


async def policy_context(
    conn: AsyncConnection, incident: Mapping[Any, Any], dx: Diagnosis | None, settings: Settings, now: datetime
) -> PolicyContext:
    rows = await conn.execute(
        text("SELECT id, tool, args FROM evidence WHERE incident_id = :id"), {"id": incident["id"]}
    )
    return PolicyContext(
        incident_service=incident["service"],
        root_causes=tuple((rc.service, rc.confidence) for rc in dx.root_causes) if dx else (),
        evidence={r.id: EvidenceRef(r.tool, dict(r.args)) for r in rows},
        namespace=settings.k8s_namespace,
        allowed_targets=frozenset(settings.remediation_targets),
        min_confidence=settings.remediation_min_confidence,
        auto_max_risk=settings.remediation_auto_max_risk,
        change_window=settings.remediation_change_window,
        now=now,
    )


async def review(
    conn: AsyncConnection,
    incident: Mapping[Any, Any],
    plan: RemediationPlan,
    settings: Settings,
    *,
    stage: str = "submit",
) -> Decision:
    """The policy's verdict on a plan for this incident, now. ``stage``: "submit" when the plan
    is proposed, "execute" when the executor checks it again."""
    dx = await diagnosis(conn, incident["id"])
    with tracing.TRACER.start_as_current_span("policy.evaluate") as span:
        decision = evaluate(plan, await policy_context(conn, incident, dx, settings, datetime.now(UTC)))
        failed = [c.name for c in decision.checks if not c.ok]
        span.set_attributes(
            {
                "sre.policy.stage": stage,
                "sre.policy.verdict": decision.verdict,
                "sre.policy.risk": decision.risk or "",
                "sre.policy.actions": [f"{a.action} {a.deployment}" for a in plan.actions],
                "sre.policy.failed_checks": failed,
            }
        )
    metrics.after_commit(conn, metrics.POLICY.labels(stage, decision.verdict, decision.risk or "none").inc)
    return decision


async def current_plan(conn: AsyncConnection, incident_id: UUID) -> PlanRow | None:
    row = (
        await conn.execute(
            text("SELECT * FROM remediation_plans WHERE incident_id = :id ORDER BY seq DESC LIMIT 1"),
            {"id": incident_id},
        )
    ).one_or_none()
    return PlanRow.from_row(row) if row else None


async def plans(conn: AsyncConnection, incident_id: UUID) -> list[PlanRow]:
    rows = await conn.execute(
        text("SELECT * FROM remediation_plans WHERE incident_id = :id ORDER BY seq"), {"id": incident_id}
    )
    return [PlanRow.from_row(r) for r in rows]


async def submit(
    conn: AsyncConnection,
    incident: Mapping[Any, Any],
    plan: RemediationPlan,
    *,
    proposed_by: str,
    actor: str,
    settings: Settings,
) -> tuple[PlanRow, str]:
    """Record a plan and the policy's verdict, and move the incident on. Returns (plan, new state).

    ``incident`` must be at DIAGNOSED (the planner's plan) or AWAITING_APPROVAL (a revision).
    """
    decision = await review(conn, incident, plan, settings)
    seq = 1 + int(
        await conn.scalar(
            text("SELECT COALESCE(MAX(seq), 0) FROM remediation_plans WHERE incident_id = :id"),
            {"id": incident["id"]},
        )
    )
    digest = plan_hash(incident["id"], seq, plan)
    expires = (
        datetime.now(UTC) + timedelta(seconds=settings.remediation_approval_ttl_s)
        if decision.verdict == "require_approval"
        else None
    )
    row = (
        await conn.execute(
            text(
                """
                INSERT INTO remediation_plans (tenant_id, incident_id, seq, proposed_by, plan, plan_hash, verdict,
                                               risk, checks, expires_at)
                VALUES (:tenant, :incident, :seq, :by, CAST(:plan AS jsonb), :hash, :verdict, :risk,
                        CAST(:checks AS jsonb), :expires)
                RETURNING *
                """
            ),
            {
                "tenant": incident["tenant_id"],
                "incident": incident["id"],
                "seq": seq,
                "by": proposed_by,
                "plan": plan.model_dump_json(),
                "hash": digest,
                "verdict": decision.verdict,
                "risk": decision.risk,
                "checks": json.dumps(decision.checks_json()),
                "expires": expires,
            },
        )
    ).one()
    saved = PlanRow.from_row(row)
    await audit.append(
        conn,
        actor=actor,
        event="remediation.plan",
        subject=str(incident["id"]),
        tenant_id=incident["tenant_id"],
        payload={
            "plan_id": saved.id,
            "seq": seq,
            "plan_hash": digest,
            "proposed_by": proposed_by,
            "actions": describe(plan),
            "verdict": decision.verdict,
            "risk": decision.risk,
            "checks": decision.checks_json(),
        },
    )

    version = await incidents.transition(
        conn,
        incident["id"],
        from_state=incident["state"],
        to_state="PLANNED",
        expected_version=incident["version"],
        actor=actor,
        reason=_plan_line(plan)[:500],
    )
    version = await incidents.transition(
        conn,
        incident["id"],
        from_state="PLANNED",
        to_state="SAFETY_REVIEWED",
        expected_version=version,
        actor="policy",
        reason=f"{decision.verdict}: {decision.reasons()}"[:500],
    )
    if decision.verdict == "deny":
        reason = plan.no_action_reason if not plan.actions and plan.no_action_reason else decision.reasons()
        await incidents.transition(
            conn,
            incident["id"],
            from_state="SAFETY_REVIEWED",
            to_state="ESCALATED",
            expected_version=version,
            actor="policy",
            reason=f"no safe automatic repair: {reason}"[:500],
        )
        return saved, "ESCALATED"
    if decision.verdict == "require_approval":
        await incidents.transition(
            conn,
            incident["id"],
            from_state="SAFETY_REVIEWED",
            to_state="AWAITING_APPROVAL",
            expected_version=version,
            actor="policy",
            reason=f"{decision.risk} plan {digest[:12]} needs an approval by {expires:%H:%M} UTC"
            if expires
            else "needs an approval",
        )
        await _enqueue(
            conn, incident, "approval_timeout", saved.id, settings, delay_s=settings.remediation_approval_ttl_s
        )
        return saved, "AWAITING_APPROVAL"
    await incidents.transition(
        conn,
        incident["id"],
        from_state="SAFETY_REVIEWED",
        to_state="EXECUTING",
        expected_version=version,
        actor="policy",
        reason=f"{decision.risk} plan {digest[:12]} is allowed without an approval",
    )
    await _enqueue(conn, incident, "execute", saved.id, settings)
    return saved, "EXECUTING"


class ApprovalRefused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def decide(
    conn: AsyncConnection,
    incident: Mapping[Any, Any],
    *,
    approver: str,
    decision: str,
    hash_seen: str,
    comment: str,
    settings: Settings,
) -> str:
    """A human's approval or rejection of the plan waiting for one. Returns the new state.

    Refused (ApprovalRefused) when nothing waits, the hash is not the waiting plan's (it was
    revised since the approver looked), the decision came too late, or the approver proposed
    the plan themselves (rejecting your own plan is fine).
    """
    if incident["state"] != "AWAITING_APPROVAL":
        raise ApprovalRefused(409, f"the incident is {incident['state']}, not waiting for an approval")
    plan = await current_plan(conn, incident["id"])
    if plan is None or plan.verdict != "require_approval":
        raise ApprovalRefused(409, "no plan is waiting for an approval")
    if hash_seen != plan.plan_hash:
        raise ApprovalRefused(409, "the plan changed since you looked at it; review the current plan")
    if plan.expires_at is not None and datetime.now(UTC) >= plan.expires_at:
        raise ApprovalRefused(409, "the approval window for this plan has closed")
    if decision == "approve" and approver == plan.proposed_by:
        raise ApprovalRefused(403, "a plan cannot be approved by the person who proposed it")
    inserted = await conn.execute(
        text(
            """
            INSERT INTO approvals (tenant_id, incident_id, plan_id, plan_hash, approver, decision, comment)
            VALUES (:tenant, :incident, :plan, :hash, :approver, :decision, :comment)
            ON CONFLICT (plan_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "tenant": incident["tenant_id"],
            "incident": incident["id"],
            "plan": plan.id,
            "hash": plan.plan_hash,
            "approver": approver,
            "decision": decision,
            "comment": comment,
        },
    )
    if inserted.scalar_one_or_none() is None:
        raise ApprovalRefused(409, "this plan has already been decided")
    waited = (datetime.now(UTC) - plan.created_at).total_seconds()
    metrics.after_commit(conn, partial(metrics.APPROVAL_WAIT.labels(decision).observe, waited))
    tracing.event("remediation.decision", decision=decision, plan_hash=plan.plan_hash[:12], waited_s=round(waited))
    await audit.append(
        conn,
        actor=approver,
        event=f"remediation.{'approved' if decision == 'approve' else 'rejected'}",
        subject=str(incident["id"]),
        tenant_id=incident["tenant_id"],
        payload={"plan_id": plan.id, "plan_hash": plan.plan_hash, "comment": comment},
    )
    if decision == "approve":
        await incidents.transition(
            conn,
            incident["id"],
            from_state="AWAITING_APPROVAL",
            to_state="EXECUTING",
            expected_version=incident["version"],
            actor=approver,
            reason=f"plan {plan.plan_hash[:12]} approved",
        )
        await _enqueue(conn, incident, "execute", plan.id, settings)
        return "EXECUTING"
    await incidents.transition(
        conn,
        incident["id"],
        from_state="AWAITING_APPROVAL",
        to_state="ESCALATED",
        expected_version=incident["version"],
        actor=approver,
        reason=f"plan {plan.plan_hash[:12]} rejected: {comment}"[:500],
    )
    return "ESCALATED"


async def approval(conn: AsyncConnection, plan_id: int) -> Any:
    return (await conn.execute(text("SELECT * FROM approvals WHERE plan_id = :p"), {"p": plan_id})).one_or_none()


async def _enqueue(
    conn: AsyncConnection,
    incident: Mapping[Any, Any],
    kind: str,
    plan_id: int,
    settings: Settings,
    *,
    delay_s: float = 0.0,
) -> None:
    await jobs.enqueue(
        conn,
        tenant_id=incident["tenant_id"],
        incident_id=incident["id"],
        kind=kind,
        payload={"plan_id": plan_id},
        priority=incident["priority"],
        max_attempts=settings.queue_max_attempts,
        aging_step_s=settings.queue_aging_step_s,
        idempotency_key=f"plan:{plan_id}:{kind}",
        delay_s=delay_s,
    )


def _plan_line(plan: RemediationPlan) -> str:
    if not plan.actions:
        return f"no action proposed: {plan.no_action_reason or 'none given'}"
    return "proposed: " + ", ".join(f"{a.action} {a.deployment}" for a in plan.actions)
