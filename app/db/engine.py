"""Async engine factory. One engine (and connection pool) per process."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.obs import metrics


def make_engine(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
    )
    metrics.install(engine)  # observations made in a transaction wait for its commit
    return engine
