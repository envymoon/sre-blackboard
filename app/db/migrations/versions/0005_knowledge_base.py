"""knowledge base: documents and chunks (full text + embeddings)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("services", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), server_default="", nullable=False),
        sa.Column("embedder", sa.Text(), nullable=False),
        sa.Column("chunking", sa.Text(), nullable=False),
        sa.Column("ingested_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('runbook', 'postmortem')", name=op.f("ck_kb_documents_kind_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_documents")),
    )
    op.create_index("ux_kb_documents_tenant_doc", "kb_documents", ["tenant_id", "doc_id"], unique=True)
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("headings", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("sections", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', search_header), 'A') || setweight(to_tsvector('english', content), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("search_header", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["kb_documents.id"],
            name=op.f("fk_kb_chunks_document_id_kb_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kb_chunks")),
    )
    op.create_index("ux_kb_chunks_tenant_chunk", "kb_chunks", ["tenant_id", "chunk_id"], unique=True)
    op.create_index("ix_kb_chunks_document_id", "kb_chunks", ["document_id"])
    op.create_index("ix_kb_chunks_tsv", "kb_chunks", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
