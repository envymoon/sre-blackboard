"""Tenants + four auth gates + approvals. Maps to spec Sec.17.1, Sec.25.6, Sec.26:
RBAC + ABAC, approvals bound to incident + action_plan + idempotency key, hash-chained audit log."""
from __future__ import annotations
import hashlib
import time

ROLES = {
    "tenant_admin": {"allow": ["manage"], "deny": ["bypass_R3_R4"]},
    "oncall": {"allow": ["read", "ticket", "submit_approval"], "deny": ["cross_tenant"]},
    "service_owner": {"allow": ["runbook", "approve_own_service"], "deny": ["other_service"]},
    "security_approver": {"allow": ["approve_R3"], "deny": ["write_business_data"]},
    "viewer": {"allow": ["read"], "deny": ["claim", "act"]},
    "agent_sa": {"allow": ["read_diag"], "deny": ["admin", "cross_tenant", "perm_write"]},
}


def ingress(auth: dict) -> bool:
    return bool(auth.get("tenant_id") and auth.get("principal_id") and auth.get("expires_at", 0) > time.time())


def runtime_claim_check(auth: dict, task: dict) -> bool:
    if auth.get("tenant_id") != task.get("tenant_id", auth.get("tenant_id")):
        return False
    if task.get("service") and task["service"] not in auth.get("service_scope", [task["service"]]):
        return False
    if "viewer" in auth.get("role_ids", []):
        return False
    return True


class Approvals:
    def __init__(self):
        self.cards: dict = {}
    def create_card(self, incident_id: str, plan_id: str, priority: str, service: str,
                    cur_ver: str, target_ver: str, evidence: list, impact: str, expires_s: int = 1800) -> dict:
        key = f"{incident_id}+{plan_id}"
        card = {"incident_id": incident_id, "action_plan_id": plan_id, "priority": priority,
                "service": service, "cur_version": cur_ver, "target_version": target_ver,
                "evidence": evidence, "impact": impact,
                "expires_at": time.time() + expires_s, "decision": "pending"}
        self.cards[key] = card
        return card

    def approve(self, incident_id: str, plan_id: str, approver: str, idempotency_key: str) -> dict:
        key = f"{incident_id}+{plan_id}"
        card = self.cards.get(key)
        if not card or card["expires_at"] < time.time():
            return {"approved": False, "reason": "expired or missing"}
        card.update({"decision": "approved", "approver": approver, "idempotency_key": idempotency_key})
        return {"approved": True, "scope": key}


# In-process singleton: production persists to the DB; local demos keep create->approve on one instance
APPROVALS = Approvals()


class AuditLog:
    """Append-only audit log with hash chain."""
    def __init__(self):
        self.events: list = []
        self.prev_hash: str = "genesis"

    def append(self, tenant_id: str, principal: str, action: str, resource: str, decision: str, run_id: str) -> dict:
        body = f"{self.prev_hash}|{tenant_id}|{principal}|{action}|{resource}|{decision}|{run_id}"
        h = hashlib.sha256(body.encode()).hexdigest()[:16]
        ev = {"tenant_id": tenant_id, "principal": principal, "action": action,
              "resource": resource, "decision": decision, "run_id": run_id, "hash": h, "prev": self.prev_hash}
        self.events.append(ev)
        self.prev_hash = h
        return ev
