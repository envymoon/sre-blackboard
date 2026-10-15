"""llm call ledger, agent run checkpoints, blackboard

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = postgresql.TIMESTAMP(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=True),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("step_no", sa.SmallInteger(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_creation_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("step", JSONB, nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('ok', 'error')", name=op.f("ck_llm_calls_status_valid")),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name=op.f("fk_llm_calls_incident_id_incidents")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index("ix_llm_calls_incident_id", "llm_calls", ["incident_id"], unique=False)

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("steps", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("result", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint("status IN ('running', 'done', 'failed')", name=op.f("ck_agent_runs_status_valid")),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name=op.f("fk_agent_runs_incident_id_incidents")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index("ux_agent_runs_incident_agent", "agent_runs", ["incident_id", "agent"], unique=True)

    op.create_table(
        "blackboard",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("body", JSONB, nullable=False),
        sa.Column("evidence_ids", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('plan', 'finding', 'diagnosis')", name=op.f("ck_blackboard_kind_valid")),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name=op.f("fk_blackboard_incident_id_incidents")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blackboard")),
    )
    op.create_index("ix_blackboard_incident_id", "blackboard", ["incident_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_blackboard_incident_id", table_name="blackboard")
    op.drop_table("blackboard")
    op.drop_index("ux_agent_runs_incident_agent", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_llm_calls_incident_id", table_name="llm_calls")
    op.drop_table("llm_calls")
