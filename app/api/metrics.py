"""``GET /metrics`` and the request middleware that counts and traces API calls.

Not behind a token: Prometheus scrapes it on the internal network, and it holds counts only
(no tenant, incident or service labels). Put it behind the network policy, not on the internet.
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from typing import Any

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.obs import metrics, state

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def scrape(request: Request) -> Response:
    body = generate_latest(metrics.registry())
    try:
        async with request.app.state.engine.connect() as conn:
            body += await state.render(conn)
    except Exception:
        body += await state.render(None)
    return Response(body, media_type=CONTENT_TYPE_LATEST)


class RequestMetrics:
    """Counts and times every request by its route template (``/api/v1/incidents/{incident_id}``,
    not the id). The request spans are FastAPI's own (app/api/app.py)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = {"code": 500}

        async def send_status(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            route = _route(scope)
            metrics.HTTP_REQUESTS.labels(scope["method"], route, str(status["code"])).inc()
            metrics.HTTP_SECONDS.labels(scope["method"], route).observe(time.monotonic() - started)


def untraced(scope: MutableMapping[str, Any]) -> bool:
    """Requests FastAPI should not make spans for: the change stream stays open for minutes,
    the probes and the scrape run every few seconds."""
    path: str = scope.get("path", "")
    return not path.startswith("/api/") or path.startswith("/api/v1/stream")


def _route(scope: MutableMapping[str, Any]) -> str:
    route = scope.get("route")
    return getattr(route, "path", None) or "unmatched"
