"""Plans, approvals and the audit trail (ADR-0007). Every query is scoped to the caller's tenant.

A human decides on the plan the policy put in front of them, identified by its hash: if the
plan was revised after they looked, their decision is refused instead of applying to a plan
they never saw. Nobody approves a plan they proposed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.security import Principal, require
from app.db import audit, incidents
from app.db.incidents import IllegalTransition, StaleVersion
from app.remediation import flow
from app.remediation.plans import RemediationPlan

router = APIRouter(prefix="/api/v1", tags=["remediation"])


def actor(principal: Principal) -> str:
    return f"user:{principal.subject}"


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="The hash of the plan you reviewed")
    decision: Literal["approve", "reject"]
    comment: str = Field("", max_length=500)


async def _incident(conn: Any, incident_id: UUID, principal: Principal) -> Any:
    row = await incidents.get(conn, incident_id, tenant_id=principal.tenant_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    return row


@router.get("/incidents/{incident_id}/plans")
async def list_plans(
    incident_id: UUID, request: Request, principal: Annotated[Principal, Depends(require("viewer"))]
) -> list[dict[str, Any]]:
    """Every plan for the incident, oldest first, with the policy's checks, the decision on it
    and what the executor did."""
    async with request.app.state.engine.connect() as conn:
        await _incident(conn, incident_id, principal)
        out = []
        for p in await flow.plans(conn, incident_id):
            decided = await flow.approval(conn, p.id)
            runs = await conn.execute(
                text(
                    "SELECT action_index, action, status, executor, dry_run, before, after, error, started_at, "
                    "finished_at FROM executions WHERE plan_id = :p ORDER BY action_index"
                ),
                {"p": p.id},
            )
            out.append(
                {
                    "id": p.id,
                    "seq": p.seq,
                    "plan_hash": p.plan_hash,
                    "proposed_by": p.proposed_by,
                    "plan": p.plan.model_dump(mode="json"),
                    "verdict": p.verdict,
                    "risk": p.risk,
                    "checks": p.checks,
                    "expires_at": p.expires_at,
                    "created_at": p.created_at,
                    "approval": None
                    if decided is None
                    else {
                        "approver": decided.approver,
                        "decision": decided.decision,
                        "comment": decided.comment,
                        "at": decided.created_at,
                    },
                    "executions": [dict(r._mapping) for r in runs],
                }
            )
    return out


@router.post("/incidents/{incident_id}/plans", status_code=status.HTTP_201_CREATED)
async def revise_plan(
    incident_id: UUID,
    plan: RemediationPlan,
    request: Request,
    principal: Annotated[Principal, Depends(require("responder"))],
) -> dict[str, Any]:
    """Replace the plan waiting for an approval with your own. It is reviewed by the same policy;
    approvals of the old plan do not carry over, and you cannot approve this one yourself."""
    who = actor(principal)
    async with request.app.state.engine.begin() as conn:
        row = await _incident(conn, incident_id, principal)
        if row["state"] != "AWAITING_APPROVAL":
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"the incident is {row['state']}; only a waiting plan can be revised"
            )
        try:
            saved, state = await flow.submit(
                conn, row, plan, proposed_by=who, actor=who, settings=request.app.state.settings
            )
        except (StaleVersion, IllegalTransition) as e:
            raise HTTPException(status.HTTP_409_CONFLICT, f"the incident changed meanwhile: {e}") from e
    return {
        "plan_id": saved.id,
        "plan_hash": saved.plan_hash,
        "verdict": saved.verdict,
        "checks": saved.checks,
        "state": state,
    }


@router.post("/incidents/{incident_id}/approvals")
async def decide(
    incident_id: UUID,
    body: ApprovalIn,
    request: Request,
    principal: Annotated[Principal, Depends(require("approver"))],
) -> dict[str, Any]:
    async with request.app.state.engine.begin() as conn:
        row = await _incident(conn, incident_id, principal)
        try:
            state = await flow.decide(
                conn,
                row,
                approver=actor(principal),
                decision=body.decision,
                hash_seen=body.plan_hash,
                comment=body.comment,
                settings=request.app.state.settings,
            )
        except flow.ApprovalRefused as e:
            raise HTTPException(e.status, str(e)) from e
        except (StaleVersion, IllegalTransition) as e:
            raise HTTPException(status.HTTP_409_CONFLICT, f"the incident changed meanwhile: {e}") from e
    return {"state": state}


@router.get("/incidents/{incident_id}/audit")
async def incident_audit(
    incident_id: UUID, request: Request, principal: Annotated[Principal, Depends(require("viewer"))]
) -> list[dict[str, Any]]:
    """The incident's audit trail: every state move, plan, decision, execution and check, in order."""
    async with request.app.state.engine.connect() as conn:
        await _incident(conn, incident_id, principal)
        rows = await conn.execute(
            text(
                "SELECT seq, ts, actor, event, payload, hash FROM audit_log "
                "WHERE subject = :s AND tenant_id = :t ORDER BY seq"
            ),
            {"s": str(incident_id), "t": principal.tenant_id},
        )
        return [dict(r._mapping) for r in rows]


class VerificationOut(BaseModel):
    ok: bool
    checked: int
    first_bad_seq: int | None
    reason: str
    at: datetime


@router.get("/audit/verify")
async def verify_audit(request: Request, principal: Annotated[Principal, Depends(require("admin"))]) -> VerificationOut:
    """Recompute the whole hash chain. Any edited, deleted or reordered row shows up here."""
    async with request.app.state.engine.connect() as conn:
        v = await audit.verify(conn)
        now = await conn.scalar(text("SELECT now()"))
    return VerificationOut(ok=v.ok, checked=v.checked, first_bad_seq=v.first_bad_seq, reason=v.reason, at=now)
