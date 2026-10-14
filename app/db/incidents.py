"""Incident repository: idempotent creation and guarded state transitions."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from . import audit
from .schema import incidents

TERMINAL = frozenset({"RESOLVED", "ESCALATED"})

# Allowed moves of the incident state machine. ESCALATED is reachable from every
# non-terminal state (budget exhausted, not enough evidence, approval refused, ...).
TRANSITIONS: dict[str, frozenset[str]] = {
    "RECEIVED": frozenset({"TRIAGED"}),
    "TRIAGED": frozenset({"INVESTIGATING"}),
    "INVESTIGATING": frozenset({"DIAGNOSED"}),
    "DIAGNOSED": frozenset({"PLANNED", "INVESTIGATING"}),
    "PLANNED": frozenset({"SAFETY_REVIEWED"}),
    "SAFETY_REVIEWED": frozenset({"AWAITING_APPROVAL", "EXECUTING"}),
    "AWAITING_APPROVAL": frozenset({"EXECUTING"}),
    "EXECUTING": frozenset({"VERIFYING"}),
    "VERIFYING": frozenset({"RESOLVED"}),
}


class IllegalTransition(Exception):
    pass


class StaleVersion(Exception):
    """The incident changed since the caller read it (optimistic lock failed)."""


def check_transition(from_state: str, to_state: str) -> None:
    if from_state in TERMINAL:
        raise IllegalTransition(f"{from_state} is terminal")
    if to_state != "ESCALATED" and to_state not in TRANSITIONS.get(from_state, frozenset()):
        raise IllegalTransition(f"{from_state} -> {to_state}")


async def open_or_merge(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    fingerprint: str,
    title: str,
    service: str,
    environment: str,
    priority: int,
    labels: dict[str, Any],
) -> tuple[UUID, bool]:
    """Return the open incident for this fingerprint, creating it if there is none.

    The partial unique index on open incidents makes this safe under concurrency: of many
    concurrent inserts exactly one succeeds, the rest fall through to the merge.
    Returns ``(incident_id, created)``.
    """
    for _ in range(3):
        inserted = await conn.execute(
            text(
                """
                INSERT INTO incidents (tenant_id, fingerprint, title, service, environment, priority, labels)
                VALUES (:tenant_id, :fp, :title, :service, :env, :priority, CAST(:labels AS jsonb))
                ON CONFLICT (tenant_id, fingerprint) WHERE closed_at IS NULL DO NOTHING
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "fp": fingerprint,
                "title": title,
                "service": service,
                "env": environment,
                "priority": priority,
                "labels": json.dumps(labels),
            },
        )
        incident_id = inserted.scalar_one_or_none()
        if incident_id is not None:
            return incident_id, True
        merged = await conn.execute(
            text(
                """
                UPDATE incidents
                SET alert_count = alert_count + 1, priority = LEAST(priority, :priority), updated_at = now()
                WHERE tenant_id = :tenant_id AND fingerprint = :fp AND closed_at IS NULL
                RETURNING id
                """
            ),
            {"tenant_id": tenant_id, "fp": fingerprint, "priority": priority},
        )
        incident_id = merged.scalar_one_or_none()
        if incident_id is not None:
            return incident_id, False
        # The open incident closed between the two statements; try again.
    raise RuntimeError(f"could not open or merge incident for fingerprint {fingerprint}")


async def transition(
    conn: AsyncConnection,
    incident_id: UUID,
    *,
    from_state: str,
    to_state: str,
    expected_version: int,
    actor: str,
    reason: str = "",
) -> int:
    """Move an incident to a new state. Returns the new version.

    Raises IllegalTransition for a move the state machine does not allow, and StaleVersion
    if the incident is no longer at ``(from_state, expected_version)``.
    """
    check_transition(from_state, to_state)
    result = await conn.execute(
        text(
            """
            UPDATE incidents
            SET state = :to_state, version = version + 1, updated_at = now(),
                closed_at = CASE WHEN :terminal THEN now() ELSE closed_at END
            WHERE id = :id AND state = :from_state AND version = :version
            RETURNING tenant_id, version
            """
        ),
        {
            "id": incident_id,
            "from_state": from_state,
            "to_state": to_state,
            "version": expected_version,
            "terminal": to_state in TERMINAL,
        },
    )
    row = result.one_or_none()
    if row is None:
        raise StaleVersion(f"incident {incident_id} is not at {from_state} v{expected_version}")
    await audit.append(
        conn,
        actor=actor,
        event="incident.transition",
        subject=str(incident_id),
        tenant_id=row.tenant_id,
        payload={"from": from_state, "to": to_state, "version": row.version, "reason": reason},
    )
    return int(row.version)


async def get(conn: AsyncConnection, incident_id: UUID, *, tenant_id: str | None) -> RowMapping | None:
    """Fetch one incident. Pass the caller's tenant; ``None`` is for internal callers only."""
    query = select(incidents).where(incidents.c.id == incident_id)
    if tenant_id is not None:
        query = query.where(incidents.c.tenant_id == tenant_id)
    return (await conn.execute(query)).mappings().one_or_none()


async def list_for_tenant(conn: AsyncConnection, tenant_id: str, *, limit: int = 50) -> list[RowMapping]:
    query = (
        select(incidents).where(incidents.c.tenant_id == tenant_id).order_by(incidents.c.created_at.desc()).limit(limit)
    )
    return list((await conn.execute(query)).mappings())
