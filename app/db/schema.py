"""Table definitions (SQLAlchemy Core). Alembic migrations are written against this metadata."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

TS = TIMESTAMP(timezone=True)

INCIDENT_STATES = (
    "RECEIVED",
    "TRIAGED",
    "INVESTIGATING",
    "DIAGNOSED",
    "PLANNED",
    "SAFETY_REVIEWED",
    "AWAITING_APPROVAL",
    "EXECUTING",
    "VERIFYING",
    "RESOLVED",
    "ESCALATED",
)
JOB_STATUSES = ("queued", "running", "done", "dead")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


incidents = Table(
    "incidents",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("tenant_id", Text, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("service", Text, nullable=False),
    Column("environment", Text, nullable=False),
    Column("priority", SmallInteger, nullable=False),
    Column("state", Text, nullable=False, server_default="RECEIVED"),
    # Optimistic lock: every state change bumps it and is conditional on the old value.
    Column("version", Integer, nullable=False, server_default="1"),
    Column("alert_count", Integer, nullable=False, server_default="1"),
    Column("labels", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
    Column("closed_at", TS),
    CheckConstraint("priority BETWEEN 0 AND 3", name="priority_range"),
    CheckConstraint(_in("state", INCIDENT_STATES), name="state_valid"),
    # Idempotent ingest: at most one open incident per (tenant, fingerprint).
    Index(
        "ux_incidents_open_fingerprint",
        "tenant_id",
        "fingerprint",
        unique=True,
        postgresql_where=text("closed_at IS NULL"),
    ),
    Index("ix_incidents_tenant_created", "tenant_id", "created_at"),
)

alerts = Table(
    "alerts",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id")),
    Column("fingerprint", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("labels", JSONB, nullable=False),
    Column("annotations", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("starts_at", TS),
    Column("ends_at", TS),
    Column("received_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('firing', 'resolved')", name="status_valid"),
    Index("ix_alerts_incident_id", "incident_id"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id")),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    # Enqueueing twice with the same key is a no-op, so handlers can enqueue follow-ups safely on retry.
    Column("idempotency_key", Text, unique=True),
    Column("priority", SmallInteger, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False),
    # Claim order: enqueue time pushed back by priority * aging step. Fixed at enqueue time,
    # so a job keeps both its priority and its place in line when a lease expires.
    Column("sort_key", TS, nullable=False),
    Column("run_after", TS, nullable=False, server_default=func.now()),
    Column("lease_owner", Text),
    Column("lease_until", TS),
    # Incremented on every claim. Writes from a worker must carry the fence it claimed with.
    Column("fence", BigInteger, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("enqueued_at", TS, nullable=False, server_default=func.now()),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint("priority BETWEEN 0 AND 3", name="priority_range"),
    CheckConstraint(_in("status", JOB_STATUSES), name="status_valid"),
    Index("ix_jobs_claimable", "sort_key", postgresql_where=text("status = 'queued'")),
    Index("ix_jobs_leased", "lease_until", postgresql_where=text("status = 'running'")),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("seq", BigInteger, Identity(), primary_key=True),
    Column("ts", TS, nullable=False),
    Column("tenant_id", Text),
    Column("actor", Text, nullable=False),
    Column("event", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("prev_hash", Text, nullable=False),
    Column("hash", Text, nullable=False, unique=True),
)
