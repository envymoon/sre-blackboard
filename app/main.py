"""ASGI entrypoint: ``uvicorn app.main:app``."""

from .api import app

__all__ = ["app"]
