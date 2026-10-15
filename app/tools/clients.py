"""HTTP clients for the observability backends, and how their failures map to ToolError."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import Settings
from app.tools.base import ToolError

# Statuses worth retrying: the backend may answer the same request on a later try.
_RETRYABLE = {429, 500, 502, 503, 504}


@dataclass
class Clients:
    """One client per backend. Base URLs and credentials come from Settings."""

    prometheus: httpx.AsyncClient
    loki: httpx.AsyncClient
    jaeger: httpx.AsyncClient
    flagd: httpx.AsyncClient
    k8s: httpx.AsyncClient
    namespace: str = "otel-demo"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> Clients:
        def client(base_url: str, **kw: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url=base_url, transport=transport, timeout=None, **kw)

        k8s_headers = {}
        if settings.k8s_token is not None:
            k8s_headers["Authorization"] = f"Bearer {settings.k8s_token.get_secret_value()}"
        verify: bool | str = settings.k8s_ca_file or True
        return cls(
            prometheus=client(settings.prometheus_url),
            loki=client(settings.loki_url),
            jaeger=client(settings.jaeger_url),
            flagd=client(settings.flagd_ui_url),
            k8s=client(settings.k8s_api_url, headers=k8s_headers, verify=verify),
            namespace=settings.k8s_namespace,
        )

    async def aclose(self) -> None:
        for c in (self.prometheus, self.loki, self.jaeger, self.flagd, self.k8s):
            await c.aclose()


async def get_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """GET and decode JSON. Every failure becomes a ToolError with a message a model can use."""
    try:
        resp = await client.get(path, params=params, headers=headers)
    except httpx.TimeoutException as e:
        raise ToolError("timeout", f"{client.base_url} did not answer in time", retryable=True) from e
    except httpx.TransportError as e:
        raise ToolError("unavailable", f"cannot reach {client.base_url}: {e}", retryable=True) from e
    if resp.status_code >= 400:
        detail = _error_detail(resp)
        if resp.status_code in _RETRYABLE:
            raise ToolError("backend_error", f"HTTP {resp.status_code}: {detail}", retryable=True)
        if resp.status_code == 404:
            raise ToolError("not_found", detail)
        if resp.status_code in (401, 403):
            raise ToolError("forbidden", detail)
        # 400 / 422: the query itself is wrong. The backend's message says how.
        raise ToolError("bad_query", detail)
    try:
        return resp.json()
    except ValueError as e:
        raise ToolError("backend_error", f"response is not JSON: {resp.text[:200]}") from e


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500] or resp.reason_phrase
    if isinstance(body, dict):
        for key in ("error", "message", "errors"):
            if body.get(key):
                return str(body[key])[:500]
    return str(body)[:500]
