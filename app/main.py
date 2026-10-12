"""ASGI entrypoint: ``uvicorn app.main:create_app --factory``."""

from .api.app import create_app

__all__ = ["create_app"]
