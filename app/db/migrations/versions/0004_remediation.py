"""remediation plans, approvals, executions

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = postgresql.TIMESTAMP(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
APPEND_ONLY = ("remediation_plans", "approvals")


def upgrade() -> None:
    op.create_table(
        "remediation_plans",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.SmallInteger(), nullable=False),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("plan", JSONB, nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("risk", sa.Text(), nullable=True),
        sa.Column("checks", JSONB, nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('allow', 'require_approval', 'deny')", name=op.f("ck_remediation_plans_verdict_valid")
        ),
        sa.CheckConstraint(
            "risk IS NULL OR risk IN ('R0', 'R1', 'R2', 'R3', 'R4')", name=op.f("ck_remediation_plans_risk_valid")
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], name=op.f("fk_remediation_plans_incident_id_incidents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_remediation_plans")),
        sa.UniqueConstraint("plan_hash", name=op.f("uq_remediation_plans_plan_hash")),
    )
    op.create_index("ux_remediation_plans_incident_seq", "remediation_plans", ["incident_id", "seq"], unique=True)

    op.create_table(
        "approvals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("approver", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("decision IN ('approve', 'reject')", name=op.f("ck_approvals_decision_valid")),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name=op.f("fk_approvals_incident_id_incidents")),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["remediation_plans.id"], name=op.f("fk_approvals_plan_id_remediation_plans")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.UniqueConstraint("plan_id", name=op.f("uq_approvals_plan_id")),
    )

    op.create_table(
        "executions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("action_index", sa.SmallInteger(), nullable=False),
        sa.Column("action", JSONB, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("executor", sa.Text(), nullable=False),
        sa.Column("dry_run", JSONB, nullable=True),
        sa.Column("before", JSONB, nullable=True),
        sa.Column("after", JSONB, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'unknown')", name=op.f("ck_executions_status_valid")
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name=op.f("fk_executions_incident_id_incidents")),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["remediation_plans.id"], name=op.f("fk_executions_plan_id_remediation_plans")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_executions")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_executions_idempotency_key")),
    )
    op.create_index("ix_executions_incident_id", "executions", ["incident_id"], unique=False)

    # Who proposed what, and who approved which plan, can only be added to, never rewritten.
    op.execute(
        """
        CREATE FUNCTION append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are append-only (% on id %)', TG_TABLE_NAME, TG_OP, OLD.id;
        END $$
        """
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION append_only()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.execute("DROP FUNCTION append_only()")
    op.drop_index("ix_executions_incident_id", table_name="executions")
    op.drop_table("executions")
    op.drop_table("approvals")
    op.drop_index("ux_remediation_plans_incident_seq", table_name="remediation_plans")
    op.drop_table("remediation_plans")
