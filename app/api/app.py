"""FastAPI application factory.

The API holds no state of its own: everything lives in Postgres, so any number of API
processes can run side by side. Startup fails (and the process exits) if the signing
key is missing or the database is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import __version__
from app.core.config import Settings, get_settings
from app.core.security import validate_secret
from app.db.engine import make_engine

from . import alerts, evidence, health, incidents, remediation, stream


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    validate_secret(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            app.state.engine = engine
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="sre-blackboard", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        )
    app.include_router(health.router)
    app.include_router(alerts.router)
    app.include_router(incidents.router)
    app.include_router(evidence.router)
    app.include_router(remediation.router)
    app.include_router(stream.router)
    return app
