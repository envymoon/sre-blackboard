"""HTTP ingress: identity / validation / idempotency / normalization. Never replaces the
Harness. Maps to spec Sec.28.1 plus the Sec.16.2.1 10-minute dedup window."""
from __future__ import annotations
import hashlib
import json
import time

REQUIRED_ALERT_FIELDS = ["alert_id", "tenant_id", "priority", "service_id"]

# dedup:{tenant}:{fingerprint} -> incident_id, TTL 600s (in-memory here, Redis in production)
_dedup: dict = {}


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
    _dedup[key] = (now + 600, incident_id)
    return {"status": "OK", "incident_id": incident_id, "fingerprint": fp}

try:
    from fastapi import FastAPI
    app = FastAPI(title="intelligent-sre-collaboration-assistant")

    try:  # browsers (docs/live.html, GitHub Pages) call the API cross-origin
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                           allow_headers=["*"])
    except Exception:
        pass

    from .status import STATE

    @app.post("/api/v1/alerts")
    def create_alert(alert: dict):
        result = normalize(alert)
        STATE.record_alert(result, alert)
        return result

    @app.get("/api/v1/status")
    def get_status():
        return STATE.snapshot()

    @app.get("/api/v1/incidents")
    def list_incidents():
        return {"incidents": STATE.snapshot()["incidents"]}

    @app.get("/incidents/{incident_id}")
    def get_incident(incident_id: str):
        return {"incident_id": incident_id, "stage": "dynamic_evidence_collection"}

    @app.get("/incidents/{incident_id}/report")
    def get_report(incident_id: str):
        from .reporter import build_report
        return {"incident_id": incident_id, "report": build_report({"incident_id": incident_id})}

    @app.post("/incidents/{incident_id}/approve")
    def approve(incident_id: str, body: dict):
        from .auth import APPROVALS
        if not body.get("action_plan_id") or not body.get("idempotency_key"):
            return {"incident_id": incident_id, "approved": False, "reason": "plan+idempotency required"}
        out = {"incident_id": incident_id, **APPROVALS.approve(incident_id, body["action_plan_id"], body.get("approver", ""), body["idempotency_key"])}
        if out.get("approved"):
            STATE.approve(incident_id)  # release the stage-8 gate in the runtime mirror
        return out

    @app.get("/metrics")
    def metrics():
        from .ext import backend_status
        from .observe import Monitor
        return {"backends": backend_status(), "observe": Monitor().snapshot()}
except Exception:  # smoke tests do not depend on fastapi being installed
    app = None
