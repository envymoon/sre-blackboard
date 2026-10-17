"""M5 acceptance: the human in the loop, played by the acceptance run, through the real API.

For each incident a fault opened, once its plan has been reviewed by the policy:

- the fault has a known repair (``Fault.repair``) and the plan waiting for an approval is
  exactly that repair: approve it, as ``lab-approver``, bound to the plan's hash;
- it waits with any other plan: reject it (a human would not approve a repair they do not
  recognise);
- the fault has no repair in the catalogue: leave it; the lab closes the incident after
  recovering the fault.

Then wait for the executor and the verifier: RESOLVED (the SLO came back) or ESCALATED.
Acceptance (plan, M5): the bad release goes alert -> diagnosis -> rollback proposed ->
approved -> rolled back -> SLO recovered -> RESOLVED, with a complete audit trail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.core.security import mint_token
from scripts.lab.diagnose import DiagnosisSeen
from scripts.lab.faults import Fault

APPROVER = "lab-approver"
FINAL = ("RESOLVED", "ESCALATED")
# The audit events a closed loop leaves behind, in this order.
LOOP_EVENTS = (
    "remediation.plan",
    "remediation.approved",
    "execution.started",
    "execution.succeeded",
    "verification.passed",
)


@dataclass
class RemediationSeen:
    incident_id: str
    service: str
    plan: list[str] = field(default_factory=list)  # "rollout_undo checkout"
    verdict: str | None = None
    risk: str | None = None
    failed_checks: list[str] = field(default_factory=list)
    decision: str | None = None  # approve | reject | None (left waiting)
    state: str = ""
    reason: str = ""
    approved_to_final_s: float | None = None
    executions: list[str] = field(default_factory=list)  # "succeeded: roll checkout back ..."
    events: list[str] = field(default_factory=list)

    @property
    def closed_loop(self) -> bool:
        """Approved, executed, verified and resolved, with every step in the audit trail in order."""
        it = iter(self.events)
        in_order = all(e in it for e in LOOP_EVENTS)
        return self.decision == "approve" and self.state == "RESOLVED" and in_order


class Remediator:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        tenant: str,
        api_url: str = "http://localhost:8000",
        timeout_s: float = 900,
    ) -> None:
        self.engine = engine
        self.tenant = tenant
        self.timeout_s = timeout_s
        self.http = httpx.Client(base_url=api_url, timeout=30)
        self._viewer = self._headers(settings, "lab-viewer", "viewer")
        self._approver = self._headers(settings, APPROVER, "approver")
        self._admin = self._headers(settings, "lab-admin", "admin")

    def _headers(self, settings: Settings, subject: str, role: str) -> dict[str, str]:
        token = mint_token(settings, subject=subject, tenant_id=self.tenant, roles=[role], ttl_s=6 * 3600)
        return {"Authorization": f"Bearer {token}"}

    def run(self, fault: Fault, diagnoses: list[DiagnosisSeen], log: Any) -> list[RemediationSeen]:
        seen = []
        approved: dict[str, float] = {}
        for d in diagnoses:
            r = RemediationSeen(d.incident_id, d.service, state=d.state)
            plans = self._get(f"/api/v1/incidents/{d.incident_id}/plans")
            if plans:
                plan = plans[-1]
                r.plan = [f"{a['action']} {a['deployment']}" for a in plan["plan"]["actions"]]
                r.verdict, r.risk = plan["verdict"], plan["risk"]
                r.failed_checks = [f"{c['name']}: {c['detail']}" for c in plan["checks"] if not c["ok"]]
                if d.state == "AWAITING_APPROVAL" and fault.repair is not None:
                    wanted = [f"{fault.repair[0]} {fault.repair[1]}"]
                    r.decision = "approve" if r.plan == wanted else "reject"
                    comment = (
                        "lab: the expected repair"
                        if r.decision == "approve"
                        else f"lab: expected {wanted[0]}, not {', '.join(r.plan) or 'nothing'}"
                    )
                    resp = self.http.post(
                        f"/api/v1/incidents/{d.incident_id}/approvals",
                        json={"plan_hash": plan["plan_hash"], "decision": r.decision, "comment": comment},
                        headers=self._approver,
                    )
                    resp.raise_for_status()
                    approved[d.incident_id] = time.monotonic()
            log(
                f"[{fault.name}] plan for {d.title}/{d.service}: {', '.join(r.plan) or 'no action'} "
                f"-> {r.verdict or 'none'}{f' ({r.risk})' if r.risk else ''}"
                + (f"; {r.decision}d by {APPROVER}" if r.decision else "")
                + (f"; failed checks: {'; '.join(r.failed_checks)}" if r.failed_checks and r.verdict == "deny" else "")
            )
            seen.append(r)

        deadline = time.monotonic() + self.timeout_s
        waiting = {r.incident_id for r in seen if r.decision == "approve"}
        while waiting and time.monotonic() < deadline:
            states = self._states(waiting)
            for incident_id, state in states.items():
                if state in FINAL:
                    waiting.discard(incident_id)
            if waiting:
                time.sleep(10)
        for r in seen:
            if r.decision is None:
                continue
            r.state, r.reason = self._last(r.incident_id)
            if r.incident_id in approved and r.state in FINAL:
                r.approved_to_final_s = round(time.monotonic() - approved[r.incident_id], 1)
            trail = self._get(f"/api/v1/incidents/{r.incident_id}/audit")
            r.events = [e["event"] for e in trail if e["event"] != "incident.transition"]
            plans = self._get(f"/api/v1/incidents/{r.incident_id}/plans")
            r.executions = [
                f"{x['status']}: {x['action']['action']} {x['action']['deployment']} "
                f"(revision {(x.get('before') or {}).get('revision')} -> {(x.get('after') or {}).get('revision')})"
                + (f": {x['error']}" if x.get("error") else "")
                for p in plans
                for x in p["executions"]
            ]
            log(
                f"[{fault.name}] {r.service}: {r.state} ({r.reason})"
                + (f" {r.approved_to_final_s:.0f}s after the decision" if r.approved_to_final_s is not None else "")
            )
            for x in r.executions:
                log(f"[{fault.name}] execution {x}")
        return seen

    def verify_audit(self) -> dict[str, Any]:
        result: dict[str, Any] = self._get("/api/v1/audit/verify", headers=self._admin)
        return result

    def _get(self, path: str, headers: dict[str, str] | None = None) -> Any:
        resp = self.http.get(path, headers=headers or self._viewer)
        resp.raise_for_status()
        return resp.json()

    def _states(self, ids: set[str]) -> dict[str, str]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id::text AS id, state FROM incidents WHERE id::text = ANY(:ids)"), {"ids": list(ids)}
            )
            return {r.id: r.state for r in rows}

    def _last(self, incident_id: str) -> tuple[str, str]:
        with self.engine.connect() as conn:
            state = conn.scalar(text("SELECT state FROM incidents WHERE id::text = :id"), {"id": incident_id})
            reason = conn.scalar(
                text(
                    "SELECT payload->>'reason' FROM audit_log WHERE subject = :id AND event = 'incident.transition' "
                    "ORDER BY seq DESC LIMIT 1"
                ),
                {"id": incident_id},
            )
        return str(state), str(reason or "")
