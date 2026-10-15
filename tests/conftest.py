"""Shared fixtures. Database tests run against a real Postgres.

Set SRE_TEST_DATABASE_URL to use an existing database; otherwise a throwaway container is
started with testcontainers (image overridable with SRE_TEST_PG_IMAGE).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings

TEST_JWT_SECRET = "test-secret-that-is-at-least-32-bytes-long"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("SRE_TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    image = os.environ.get("SRE_TEST_PG_IMAGE", "pgvector/pgvector:pg16")
    with PostgresContainer(image, username="sre", password="sre", dbname="sre", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_url(database_url: str) -> str:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.attributes["configure_logger"] = False
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return database_url


@pytest.fixture
def settings(migrated_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=migrated_url,
        jwt_secret=TEST_JWT_SECRET,
        db_pool_size=20,
    )


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(settings.database_url, pool_size=settings.db_pool_size)
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE executions, approvals, remediation_plans, blackboard, agent_runs, llm_calls, tool_calls, evidence, jobs, alerts, incidents, audit_log RESTART IDENTITY CASCADE"
            )
        )
    yield eng
    await eng.dispose()
