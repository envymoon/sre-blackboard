"""HTTP API against a real Postgres: auth, idempotent ingest, tenant isolation, CORS, startup."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.api.app import create_app
from app.core.config import Settings
from app.core.security import mint_token
from app.db import audit
from app.ingest import identify, priority_of


def alert(status: str = "firing", **labels: str) -> dict[str, Any]:
    return {
        "status": status,
        "labels": {"alertname": "HighErrorRate", "service": "checkout", "environment": "prod", **labels},
        "annotations": {"summary": "5xx ratio above 5%"},
        "startsAt": "2026-10-12T10:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
    }


def webhook(*alerts: dict[str, Any]) -> dict[str, Any]:
    return {"version": "4", "status": alerts[0]["status"], "receiver": "sre", "alerts": list(alerts)}


def auth(settings: Settings, *roles: str, tenant: str = "t1") -> dict[str, str]:
    token = mint_token(settings, subject=f"{tenant}-{'-'.join(roles)}", tenant_id=tenant, roles=list(roles))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(engine, settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings.model_copy(update={"cors_origins": ["https://console.example"]}))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def count(engine, table: str) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()


async def test_probes(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).json() == {"status": "ok"}


async def test_requests_without_valid_credentials_are_rejected(client, settings):
    """B4: the prototype's approval endpoint had no authentication at all."""
    assert (await client.post("/api/v1/alerts", json=webhook(alert()))).status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert (await client.get("/api/v1/incidents", headers=bad)).status_code == 401
    # Authenticated, but the wrong role for the endpoint.
    assert (
        await client.post("/api/v1/alerts", json=webhook(alert()), headers=auth(settings, "admin"))
    ).status_code == 403
    assert (await client.get("/api/v1/incidents", headers=auth(settings, "ingest"))).status_code == 403


async def test_first_alert_opens_an_incident_with_a_triage_job(client, engine, settings):
    resp = await client.post("/api/v1/alerts", json=webhook(alert(priority="P0")), headers=auth(settings, "ingest"))
    assert resp.status_code == 202
    [result] = resp.json()["alerts"]
    assert result["created"] is True

    incident = (await client.get(f"/api/v1/incidents/{result['incident_id']}", headers=auth(settings, "viewer"))).json()
    assert incident["state"] == "RECEIVED" and incident["priority"] == "P0" and incident["alert_count"] == 1
    async with engine.connect() as conn:
        job = (await conn.execute(text("SELECT kind, priority, incident_id FROM jobs"))).one()
        assert (job.kind, job.priority, str(job.incident_id)) == ("triage", 0, result["incident_id"])
        events = (await conn.execute(text("SELECT event, actor FROM audit_log"))).all()
        assert [tuple(e) for e in events] == [("incident.created", "t1-ingest")]
        assert (await audit.verify(conn)).ok


async def test_repeated_alert_merges_into_the_open_incident(client, engine, settings):
    headers = auth(settings, "ingest")
    first = (await client.post("/api/v1/alerts", json=webhook(alert()), headers=headers)).json()["alerts"][0]
    again = (await client.post("/api/v1/alerts", json=webhook(alert()), headers=headers)).json()["alerts"][0]
    assert again == {**first, "created": False}
    assert await count(engine, "incidents") == 1
    assert await count(engine, "jobs") == 1
    assert await count(engine, "alerts") == 2


async def test_concurrent_duplicate_alerts_create_exactly_one_incident(client, engine, settings):
    """B3: the prototype deduplicated in process memory, so two API workers created duplicates."""
    headers = auth(settings, "ingest")
    responses = await asyncio.gather(
        *(client.post("/api/v1/alerts", json=webhook(alert()), headers=headers) for _ in range(50))
    )
    assert all(r.status_code == 202 for r in responses)
    assert sum(r.json()["alerts"][0]["created"] for r in responses) == 1
    assert len({r.json()["alerts"][0]["incident_id"] for r in responses}) == 1
    assert await count(engine, "incidents") == 1
    assert await count(engine, "jobs") == 1
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT alert_count FROM incidents"))).scalar_one() == 50


async def test_resolved_alert_does_not_open_an_incident(client, engine, settings):
    resp = await client.post("/api/v1/alerts", json=webhook(alert("resolved")), headers=auth(settings, "ingest"))
    [result] = resp.json()["alerts"]
    assert (result["status"], result["incident_id"], result["created"]) == ("resolved", None, False)
    assert await count(engine, "incidents") == 0
    assert await count(engine, "alerts") == 1


async def test_tenants_cannot_see_each_other(client, settings):
    """B6: tenant comes from the token; another tenant's incident looks like a missing one."""
    payload = webhook(alert(tenant_id="t2"))  # a label cannot choose the tenant
    created = await client.post("/api/v1/alerts", json=payload, headers=auth(settings, "ingest", tenant="t1"))
    incident_id = created.json()["alerts"][0]["incident_id"]

    own = await client.get("/api/v1/incidents", headers=auth(settings, "viewer", tenant="t1"))
    assert [i["id"] for i in own.json()] == [incident_id]
    other = auth(settings, "admin", tenant="t2")
    assert (await client.get("/api/v1/incidents", headers=other)).json() == []
    assert (await client.get(f"/api/v1/incidents/{incident_id}", headers=other)).status_code == 404


async def test_invalid_payload_is_rejected(client, settings):
    headers = auth(settings, "ingest")
    no_name = webhook({"status": "firing", "labels": {"service": "checkout"}})
    assert (await client.post("/api/v1/alerts", json=no_name, headers=headers)).status_code == 422
    empty = {"status": "firing", "alerts": []}
    assert (await client.post("/api/v1/alerts", json=empty, headers=headers)).status_code == 422


async def test_cors_only_allows_configured_origins(client):
    """B5: the prototype answered every origin with CORS *."""
    preflight = {"Access-Control-Request-Method": "GET"}
    ok = await client.options("/api/v1/incidents", headers={"Origin": "https://console.example", **preflight})
    assert ok.headers.get("access-control-allow-origin") == "https://console.example"
    evil = await client.options("/api/v1/incidents", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in evil.headers


async def test_startup_fails_when_the_database_is_unreachable(settings):
    app = create_app(settings.model_copy(update={"database_url": "postgresql+psycopg://sre:sre@127.0.0.1:1/sre"}))
    with pytest.raises(OperationalError):
        async with app.router.lifespan_context(app):
            pass


def test_alert_identity_and_priority():
    a = identify({"alertname": "HighLatency", "service_name": "cart", "namespace": "shop"})
    b = identify({"alertname": "HighLatency", "service_name": "cart", "namespace": "shop", "pod": "cart-7f9"})
    assert a == b and (a.service, a.environment) == ("cart", "shop")
    assert identify({"alertname": "HighLatency", "service": "ad"}).fingerprint != a.fingerprint
    assert priority_of({"priority": "p0", "severity": "info"}) == 0
    assert priority_of({"severity": "critical"}) == 1
    assert priority_of({"severity": "warning"}) == 2
    assert priority_of({}) == 3
