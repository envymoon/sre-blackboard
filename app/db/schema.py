"""Table definitions (SQLAlchemy Core). Alembic migrations are written against this metadata."""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, TSVECTOR, UUID

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
# The knowledge base's embedding size (app/kb/models.py: bge-small-en-v1.5).
EMBEDDING_DIM = 384

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
TOOL_CALL_STATUSES = ("ok", "error")


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
    # W3C traceparent of the incident's root span (M8): its jobs' spans are children of it.
    Column("trace_context", Text),
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
    # Copied from the incident at enqueue time (or the enqueuer's span), so the worker that
    # claims the job continues the same trace.
    Column("trace_context", Text),
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

# A snapshot of what one read-only tool call returned. Rows are immutable (a trigger rejects
# UPDATE and DELETE): findings cite them by id, and the hash proves the snapshot is unchanged.
evidence = Table(
    "evidence",
    metadata,
    # "ev_" + 16 hex characters: short enough for a model to copy back without typos.
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id")),
    Column("tool", Text, nullable=False),
    Column("args", JSONB, nullable=False),
    # The backend's answer as fetched, before compression.
    Column("result", JSONB, nullable=False),
    # sha256 of the canonical JSON of ``result``.
    Column("result_hash", Text, nullable=False),
    Column("result_bytes", Integer, nullable=False),
    # What the model was shown: a bounded summary derived from ``result``.
    Column("summary", JSONB, nullable=False),
    # The summary had to be cut to fit the size limit.
    Column("truncated", Boolean, nullable=False, server_default="false"),
    # "live" or "replay" (read from a cassette).
    Column("source", Text, nullable=False),
    # When the data was read from the backend (for a replay: when it was recorded).
    Column("fetched_at", TS, nullable=False),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint("source IN ('live', 'replay')", name="source_valid"),
    Index("ix_evidence_incident_id", "incident_id"),
)

# Every tool call, including failed ones (which leave no evidence).
tool_calls = Table(
    "tool_calls",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id")),
    Column("agent", Text),
    Column("tool", Text, nullable=False),
    Column("args", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("error_code", Text),
    Column("error", Text),
    Column("attempts", SmallInteger, nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("evidence_id", Text, ForeignKey("evidence.id")),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint(_in("status", TOOL_CALL_STATUSES), name="status_valid"),
    Index("ix_tool_calls_incident_id", "incident_id"),
)

LLM_CALL_STATUSES = ("ok", "error")
AGENT_RUN_STATUSES = ("running", "done", "failed")
BLACKBOARD_KINDS = ("plan", "finding", "diagnosis")

# Every model step, failed ones included: the usage ledger (token budget, equivalent cost)
# and, with ``step``, a recording of the run that replays without the model.
llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id")),
    Column("agent", Text, nullable=False),
    Column("step_no", SmallInteger, nullable=False),
    Column("backend", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    # sha256 of everything the model saw (app/llm/types.py LLMRequest.hash).
    Column("request_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("error_code", Text),
    Column("error", Text),
    Column("attempts", SmallInteger, nullable=False, server_default="1"),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cache_read_tokens", Integer, nullable=False, server_default="0"),
    Column("cache_creation_tokens", Integer, nullable=False, server_default="0"),
    # What the same tokens would cost on the API, as the backend reported it (the CLI does).
    Column("cost_usd", Numeric(12, 6)),
    Column("latency_ms", Integer, nullable=False),
    # The step the model returned: {"reasoning", "actions", "final"}.
    Column("step", JSONB),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint(_in("status", LLM_CALL_STATUSES), name="status_valid"),
    Index("ix_llm_calls_incident_id", "incident_id"),
)

# One row per agent per incident: the checkpoint. A job that runs again (after a crash or a
# lost lease) skips every agent whose run is done and reads its output from the blackboard.
agent_runs = Table(
    "agent_runs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False),
    Column("agent", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("steps", SmallInteger, nullable=False, server_default="0"),
    # Counts and notes about the run (tool calls, findings dropped by the citation check, ...).
    Column("result", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("error", Text),
    Column("started_at", TS, nullable=False, server_default=func.now()),
    Column("finished_at", TS),
    CheckConstraint(_in("status", AGENT_RUN_STATUSES), name="status_valid"),
    Index("ux_agent_runs_incident_agent", "incident_id", "agent", unique=True),
)

# What the agents share: the coordinator's plan, each investigator's findings, the diagnosis.
# Append-only, and every row belongs to the agent that wrote it, so agents running in
# parallel never overwrite each other. Rows are written in the same transaction that marks
# the agent's run done, so a run's output is either all there or not there at all.
blackboard = Table(
    "blackboard",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("author", Text, nullable=False),
    Column("body", JSONB, nullable=False),
    Column("evidence_ids", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint(_in("kind", BLACKBOARD_KINDS), name="kind_valid"),
    Index("ix_blackboard_incident_id", "incident_id"),
)

# --- M5: remediation (ADR-0007) ---------------------------------------------------------

POLICY_VERDICTS = ("allow", "require_approval", "deny")
RISK_LEVELS = ("R0", "R1", "R2", "R3", "R4")
APPROVAL_DECISIONS = ("approve", "reject")
EXECUTION_STATUSES = ("started", "succeeded", "failed", "unknown")

# Every plan proposed for an incident, with the policy's verdict on it. Append-only (a
# trigger rejects UPDATE and DELETE): a revised plan is a new row with the next seq, and
# its own hash, so an approval of an earlier plan never matches it.
remediation_plans = Table(
    "remediation_plans",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False),
    Column("seq", SmallInteger, nullable=False),
    # "agent:planner", or "user:<subject>" for a plan a responder submitted.
    Column("proposed_by", Text, nullable=False),
    Column("plan", JSONB, nullable=False),
    # sha256 over the incident id, seq and the canonical plan (app/remediation/plans.py).
    Column("plan_hash", Text, nullable=False, unique=True),
    Column("verdict", Text, nullable=False),
    # The highest risk among the plan's actions; NULL for a plan without actions.
    Column("risk", Text),
    # The policy's checks, one {name, ok, blocking, detail} per check.
    Column("checks", JSONB, nullable=False),
    # Approval deadline, for a plan that needs one.
    Column("expires_at", TS),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint(_in("verdict", POLICY_VERDICTS), name="verdict_valid"),
    CheckConstraint(f"risk IS NULL OR {_in('risk', RISK_LEVELS)}", name="risk_valid"),
    Index("ux_remediation_plans_incident_seq", "incident_id", "seq", unique=True),
)

# A human's decision on one plan. One per plan (the first decision stands); append-only.
approvals = Table(
    "approvals",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False),
    Column("plan_id", BigInteger, ForeignKey("remediation_plans.id"), nullable=False, unique=True),
    # The hash the approver saw; the executor checks it against the plan it is about to run.
    Column("plan_hash", Text, nullable=False),
    Column("approver", Text, nullable=False),
    Column("decision", Text, nullable=False),
    Column("comment", Text, nullable=False, server_default=""),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint(_in("decision", APPROVAL_DECISIONS), name="decision_valid"),
)

# One row per action the executor started. The unique key is written and committed before
# the cluster is touched: a rerun that finds it 'started' does not try again (at most once).
executions = Table(
    "executions",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("incident_id", UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False),
    Column("plan_id", BigInteger, ForeignKey("remediation_plans.id"), nullable=False),
    Column("action_index", SmallInteger, nullable=False),
    Column("action", JSONB, nullable=False),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("status", Text, nullable=False),
    Column("executor", Text, nullable=False),
    # The server-side dry run's answer, the target's state before, and after.
    Column("dry_run", JSONB),
    Column("before", JSONB),
    Column("after", JSONB),
    Column("error", Text),
    Column("started_at", TS, nullable=False, server_default=func.now()),
    Column("finished_at", TS),
    CheckConstraint(_in("status", EXECUTION_STATUSES), name="status_valid"),
    Index("ix_executions_incident_id", "incident_id"),
)

# --- M6: knowledge base (ADR-0010) ------------------------------------------------------

KB_KINDS = ("runbook", "postmortem")

# One row per document per tenant. Ingest is idempotent: a document whose content hash,
# embedding model and chunking are unchanged is skipped; a changed one has its chunks replaced.
kb_documents = Table(
    "kb_documents",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("doc_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("services", ARRAY(Text), nullable=False),
    Column("version", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("source_path", Text, nullable=False, server_default=""),
    Column("embedder", Text, nullable=False),
    Column("chunking", Text, nullable=False),
    Column("ingested_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint(_in("kind", KB_KINDS), name="kind_valid"),
    Index("ux_kb_documents_tenant_doc", "tenant_id", "doc_id", unique=True),
)

# Searchable pieces of a document: full text (``tsv``, generated) and an embedding. Every
# query filters on tenant_id in SQL; there is no search function without a tenant.
kb_chunks = Table(
    "kb_chunks",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("document_id", BigInteger, ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
    Column("chunk_id", Text, nullable=False),
    Column("ordinal", SmallInteger, nullable=False),
    Column("headings", ARRAY(Text), nullable=False),
    Column("sections", ARRAY(Text), nullable=False),
    Column("content", Text, nullable=False),
    Column("tokens", Integer, nullable=False),
    # Title and headings weigh more than the body (setweight A/B), as in the embedding text.
    Column(
        "tsv",
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', search_header), 'A') || setweight(to_tsvector('english', content), 'B')",
            persisted=True,
        ),
        nullable=False,
    ),
    Column("search_header", Text, nullable=False),
    Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    Index("ux_kb_chunks_tenant_chunk", "tenant_id", "chunk_id", unique=True),
    Index("ix_kb_chunks_document_id", "document_id"),
    Index("ix_kb_chunks_tsv", "tsv", postgresql_using="gin"),
)
