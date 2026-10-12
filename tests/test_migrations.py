"""The migrations and the table definitions in app/db/schema.py must agree."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection

from app.db.schema import metadata


async def test_migrations_match_schema(engine):
    def diff(conn: Connection) -> list:
        context = MigrationContext.configure(conn, opts={"compare_server_default": True})
        return compare_metadata(context, metadata)

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []
