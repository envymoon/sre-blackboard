"""Entrypoint alias: the Sec.28.5 compose file serves uvicorn app.main:app; the implementation lives in app.api."""
from .api import app  # noqa: F401
