"""evidence snapshots and tool call log

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15 02:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=True),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_hash", sa.Text(), nullable=False),
        sa.Column("result_bytes", sa.Integer(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source IN ('live', 'replay')", name=op.f("ck_evidence_source_valid")),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name=op.f("fk_evidence_incident_id_incidents")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
    )
    op.create_index("ix_evidence_incident_id", "evidence", ["incident_id"], unique=False)
    # Snapshots are immutable: a finding that cites one must keep pointing at the same data.
    op.execute(
        """
        CREATE FUNCTION evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'evidence rows are immutable (% on %)', TG_OP, OLD.id;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER evidence_immutable BEFORE UPDATE OR DELETE ON evidence "
        "FOR EACH ROW EXECUTE FUNCTION evidence_immutable()"
    )

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=True),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("evidence_id", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('ok', 'error')", name=op.f("ck_tool_calls_status_valid")),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence.id"], name=op.f("fk_tool_calls_evidence_id_evidence")),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name=op.f("fk_tool_calls_incident_id_incidents")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
    )
    op.create_index("ix_tool_calls_incident_id", "tool_calls", ["incident_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tool_calls_incident_id", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.execute("DROP TRIGGER evidence_immutable ON evidence")
    op.execute("DROP FUNCTION evidence_immutable()")
    op.drop_index("ix_evidence_incident_id", table_name="evidence")
    op.drop_table("evidence")
