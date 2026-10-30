"""trace context on incidents and jobs (one trace per incident, carried through the queue)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # W3C traceparent of the incident's root span; every job of the incident is its child.
    op.add_column("incidents", sa.Column("trace_context", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("trace_context", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "trace_context")
    op.drop_column("incidents", "trace_context")
