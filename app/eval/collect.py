"""Observations of finished incidents, read from the database (live on the lab, or replayed).

Everything comes from what the system stored while it ran: how the investigation ended
(the audit trail's transition out of INVESTIGATING), the latest diagnosis, the model calls,
each agent's run summary, the latest remediation plan and its verdict, and executions.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.eval.score import ActionObs, ExecutionObs, IncidentObs

# Agents whose final answers carry citations (the coordinator's plan has none).
CITING = ("investigator.", "synthesizer", "solo", "planner")


async def observe(engine: AsyncEngine, incident_ids: list[UUID], cause_at: datetime) -> list[IncidentObs]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, service, priority, created_at FROM incidents WHERE id = ANY(:ids) ORDER BY created_at"),
            {"ids": incident_ids},
        )
        return [await _one(conn, r, cause_at) for r in rows.all()]


async def _one(conn: AsyncConnection, row: Any, cause_at: datetime) -> IncidentObs:
    p = {"id": row.id}
    ended = (
        await conn.execute(
            text(
                "SELECT payload->>'to' AS to_state, payload->>'reason' AS reason, ts FROM audit_log "
                "WHERE subject = :s AND event = 'incident.transition' AND payload->>'from' = 'INVESTIGATING' "
                "ORDER BY seq LIMIT 1"
            ),
            {"s": str(row.id)},
        )
    ).one_or_none()
    dx = await conn.scalar(
        text("SELECT body FROM blackboard WHERE incident_id = :id AND kind = 'diagnosis' ORDER BY id DESC LIMIT 1"), p
    )
    usage = (
        await conn.execute(
            text(
                "SELECT count(*) AS calls, COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens "
                "+ cache_creation_tokens), 0) AS tokens, SUM(cost_usd) AS cost FROM llm_calls "
                "WHERE incident_id = :id AND status = 'ok'"
            ),
            p,
        )
    ).one()
    runs = (await conn.execute(text("SELECT agent, status, result FROM agent_runs WHERE incident_id = :id"), p)).all()
    findings = Counter(
        r.author
        for r in await conn.execute(
            text("SELECT author FROM blackboard WHERE incident_id = :id AND kind = 'finding'"), p
        )
    )
    plan = (
        await conn.execute(
            text("SELECT plan, verdict, risk FROM remediation_plans WHERE incident_id = :id ORDER BY seq DESC LIMIT 1"),
            p,
        )
    ).one_or_none()
    executions = (
        await conn.execute(text("SELECT action, status FROM executions WHERE incident_id = :id ORDER BY id"), p)
    ).all()

    causes = (dx or {}).get("root_causes") or []
    obs = IncidentObs(
        incident_id=str(row.id),
        service=row.service,
        priority=row.priority,
        opened_s=round((row.created_at - cause_at).total_seconds(), 1),
        outcome="pending" if ended is None else ("diagnosed" if ended.to_state == "DIAGNOSED" else "escalated"),
        reason=(ended.reason or "") if ended else "",
        root_causes=[c["service"] for c in causes],
        confidences=[c["confidence"] for c in causes],
        investigation_s=round((ended.ts - row.created_at).total_seconds(), 1) if ended else None,
        calls=usage.calls,
        tokens=int(usage.tokens),
        cost_usd=float(usage.cost) if usage.cost is not None else None,
        actions=[]
        if plan is None
        else [
            ActionObs(action=a["action"], deployment=a["deployment"], verdict=plan.verdict, risk=plan.risk)
            for a in plan.plan["actions"]
        ],
        executions=[
            ExecutionObs(action=x.action["action"], deployment=x.action["deployment"], status=x.status)
            for x in executions
        ],
    )
    for r in runs:
        if r.status != "done" or not r.agent.startswith(CITING):
            continue
        result = r.result or {}
        obs.answers += 1
        obs.first_pass += int(result.get("rewrites", 0) == 0)
        obs.dropped += int(result.get("dropped", 0))
        obs.kept += _kept(r.agent, result, findings, dx)
    return obs


def _kept(agent: str, result: dict[str, Any], findings: Counter[str], dx: dict[str, Any] | None) -> int:
    """Cited items that survived the check: findings, the diagnosis's claims, the plan's actions."""
    if agent.startswith("investigator."):
        return findings[agent]
    if agent == "planner":
        return len((result.get("plan") or {}).get("actions") or [])
    d = dx or {}
    return sum(len(d.get(k) or []) for k in ("root_causes", "facts", "inferences"))
