"""HTTP ingress: alert validation, fingerprinting and deduplication.

Dedup state is still in process memory, so the API must run as a single worker.
Milestone M1 moves it to a Postgres unique constraint and adds authentication.
"""

from __future__ import annotations

import hashlib
import json
import time

from fastapi import FastAPI

REQUIRED_ALERT_FIELDS = ["alert_id", "tenant_id", "priority", "service_id"]
DEDUP_WINDOW_S = 600

# dedup:{tenant}:{fingerprint} -> (expires_at, incident_id)
_dedup: dict[str, tuple[float, str]] = {}


def fingerprint(alert: dict) -> str:
    raw = json.dumps({k: alert.get(k) for k in ("service_id", "alert_name", "environment")}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize(alert: dict) -> dict:
    missing = [k for k in REQUIRED_ALERT_FIELDS if k not in alert]
    if missing:
        return {"status": "REJECTED", "reason": f"missing {missing}"}
    if not alert.get("auth", {}).get("tenant_id"):
        return {"status": "REJECTED", "reason": "missing AuthContext"}
    fp = fingerprint(alert)
    key = f"dedup:{alert['tenant_id']}:{fp}"
    now = time.time()
    if key in _dedup and _dedup[key][0] > now:
        return {"status": "DEDUP_MERGED", "incident_id": _dedup[key][1]}
    incident_id = f"inc-{fp[:8]}"
    _dedup[key] = (now + DEDUP_WINDOW_S, incident_id)
    return {"status": "OK", "incident_id": incident_id, "fingerprint": fp}


app = FastAPI(title="sre-blackboard")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/alerts")
def create_alert(alert: dict) -> dict:
    return normalize(alert)
