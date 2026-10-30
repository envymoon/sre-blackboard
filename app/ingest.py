"""Alert ingest: Alertmanager webhook -> alerts, incidents, triage jobs.

A firing alert opens an incident, or merges into the open incident with the same
fingerprint. Alert, incident, triage job and audit event are written in the caller's
transaction, so there is no window where an incident exists without its job.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import Settings
from app.db import audit, incidents
from app.obs import metrics, tracing
from app.queue import jobs

PRIORITY_NAMES = ("P0", "P1", "P2", "P3")
# Used when an alert has no explicit ``priority`` label. P0 is never inferred: it must be labelled.
SEVERITY_PRIORITY = {"critical": 1, "high": 1, "warning": 2, "info": 3}
DEFAULT_PRIORITY = 3


class AlertmanagerAlert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    startsAt: datetime | None = None
    endsAt: datetime | None = None
    generatorURL: str = ""
    fingerprint: str = ""

    @field_validator("labels")
    @classmethod
    def _has_alertname(cls, labels: dict[str, str]) -> dict[str, str]:
        if not labels.get("alertname"):
            raise ValueError("labels.alertname is required")
        return labels


class AlertmanagerWebhook(BaseModel):
    """The JSON body Alertmanager's webhook receiver sends (payload version 4)."""

    version: str = "4"
    status: Literal["firing", "resolved"]
    receiver: str = ""
    groupKey: str = ""
    truncatedAlerts: int = 0
    groupLabels: dict[str, str] = {}
    commonLabels: dict[str, str] = {}
    commonAnnotations: dict[str, str] = {}
    externalURL: str = ""
    alerts: list[AlertmanagerAlert] = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class AlertIdentity:
    title: str
    service: str
    environment: str
    fingerprint: str


def identify(labels: dict[str, str]) -> AlertIdentity:
    """Which incident an alert belongs to: same alert name, service and environment."""
    title = labels["alertname"]
    service = labels.get("service") or labels.get("service_name") or labels.get("job") or "unknown"
    environment = labels.get("environment") or labels.get("env") or labels.get("namespace") or "default"
    raw = json.dumps([title, service, environment], separators=(",", ":"))
    return AlertIdentity(title, service, environment, hashlib.sha256(raw.encode()).hexdigest()[:32])


def priority_of(labels: dict[str, str]) -> int:
    explicit = labels.get("priority", "").upper()
    if explicit in PRIORITY_NAMES:
        return PRIORITY_NAMES.index(explicit)
    return SEVERITY_PRIORITY.get(labels.get("severity", "").lower(), DEFAULT_PRIORITY)


@dataclass(frozen=True)
class IngestedAlert:
    fingerprint: str
    status: str
    incident_id: UUID | None
    created: bool


async def ingest(
    conn: AsyncConnection,
    webhook: AlertmanagerWebhook,
    *,
    tenant_id: str,
    actor: str,
    settings: Settings,
) -> list[IngestedAlert]:
    identified = [(identify(a.labels), a) for a in webhook.alerts]
    # A fixed lock order: two concurrent webhooks with overlapping alerts cannot deadlock.
    identified.sort(key=lambda pair: pair[0].fingerprint)
    results: list[IngestedAlert] = []
    for ident, alert in identified:
        incident_id: UUID | None
        if alert.status == "firing":
            incident_id, created = await incidents.open_or_merge(
                conn,
                tenant_id=tenant_id,
                fingerprint=ident.fingerprint,
                title=ident.title,
                service=ident.service,
                environment=ident.environment,
                priority=priority_of(alert.labels),
                labels=alert.labels,
            )
            if created:
                await _on_incident_created(conn, incident_id, ident, alert, tenant_id, actor, settings)
            result = "opened" if created else "merged"
        else:
            # Resolution is recorded against the open incident; closing it is the harness's job.
            incident_id, created = await _open_incident_id(conn, tenant_id, ident.fingerprint), False
            result = "resolved" if incident_id else "unmatched"
        await _record_alert(conn, tenant_id, incident_id, ident.fingerprint, alert)
        metrics.after_commit(conn, metrics.ALERTS.labels(alert.status, result).inc)
        results.append(IngestedAlert(ident.fingerprint, alert.status, incident_id, created))
    return results


async def _on_incident_created(
    conn: AsyncConnection,
    incident_id: UUID,
    ident: AlertIdentity,
    alert: AlertmanagerAlert,
    tenant_id: str,
    actor: str,
    settings: Settings,
) -> None:
    priority = priority_of(alert.labels)
    # The incident's own trace: every job it gets continues it (app/obs/tracing.py).
    context = tracing.open_incident(
        incident_id, tenant_id=tenant_id, service=ident.service, title=ident.title, priority=priority
    )
    if context:
        await conn.execute(
            text("UPDATE incidents SET trace_context = :c WHERE id = :id"), {"c": context, "id": incident_id}
        )
    metrics.after_commit(conn, metrics.INCIDENTS_OPENED.labels(PRIORITY_NAMES[priority]).inc)
    await jobs.enqueue(
        conn,
        tenant_id=tenant_id,
        incident_id=incident_id,
        kind="triage",
        priority=priority,
        max_attempts=settings.queue_max_attempts,
        aging_step_s=settings.queue_aging_step_s,
        idempotency_key=f"{incident_id}:triage",
    )
    await audit.append(
        conn,
        actor=actor,
        event="incident.created",
        subject=str(incident_id),
        tenant_id=tenant_id,
        payload={
            "title": ident.title,
            "service": ident.service,
            "environment": ident.environment,
            "priority": PRIORITY_NAMES[priority],
        },
    )


async def _open_incident_id(conn: AsyncConnection, tenant_id: str, fingerprint: str) -> UUID | None:
    result = await conn.execute(
        text("SELECT id FROM incidents WHERE tenant_id = :t AND fingerprint = :fp AND closed_at IS NULL"),
        {"t": tenant_id, "fp": fingerprint},
    )
    return result.scalar_one_or_none()


async def _record_alert(
    conn: AsyncConnection,
    tenant_id: str,
    incident_id: UUID | None,
    fingerprint: str,
    alert: AlertmanagerAlert,
) -> None:
    # Alertmanager sends endsAt = 0001-01-01 for alerts that are still firing.
    ends_at = alert.endsAt if alert.status == "resolved" else None
    await conn.execute(
        text(
            """
            INSERT INTO alerts (tenant_id, incident_id, fingerprint, status, labels, annotations, starts_at, ends_at)
            VALUES (:t, :incident_id, :fp, :status, CAST(:labels AS jsonb), CAST(:annotations AS jsonb),
                    :starts_at, :ends_at)
            """
        ),
        {
            "t": tenant_id,
            "incident_id": incident_id,
            "fp": fingerprint,
            "status": alert.status,
            "labels": json.dumps(alert.labels),
            "annotations": json.dumps(alert.annotations),
            "starts_at": alert.startsAt,
            "ends_at": ends_at,
        },
    )
