"""Safety + R0-R4 risk tiers. Maps to spec Sec.17.2, Sec.17.3, Sec.25.6.
The AI is read-only by default; R3 actions require human approval."""
from __future__ import annotations

RISK_LEVELS = ["R0", "R1", "R2", "R3", "R4"]


def classify_risk(action_name: str) -> str:
    if action_name in ("rollback_release", "shift_traffic", "update_config", "db_operation"):
        return "R3"
    if action_name in ("restart_stateless", "scale_out"):
        return "R2"
    if action_name in ("delete_data", "batch_change", "network_acl_change"):
        return "R4"
    return "R0"


def safety_review(artifact, action_proposal: dict, context: dict | None = None) -> dict:
    """10-item review (Sec.17.3): evidence / host match / blast radius / alternative /
    rollback / window / permission / injection / verifiability."""
    if context is None:
        return _legacy(artifact, action_proposal)
    checks = {
        "evidence": bool(artifact.evidence_refs),
        "host_match": context.get("target_host") in (context.get("problem_hosts") or [context.get("target_host")]),
        "not_event_host": context.get("target_host") != context.get("event_host") or bool(artifact.evidence_refs),
        "blast_radius": bool(context.get("blast_radius")),
        "alternative": bool(context.get("alternative_considered")),
        "rollback_plan": bool(context.get("rollback_plan")),
        "window_approval": context.get("risk") in ("R0", "R1") or bool(context.get("approval")),
        "caller_perm": bool(context.get("caller_perm", True)),
        "no_injection": not any(k in str(context.get("args", {})).lower() for k in ("token", "password", "ssh")),
        "verifiable": bool(context.get("verify_plan")),
    }
    risk = classify_risk(action_proposal.get("name", ""))
    critical_fail = not checks["evidence"] or not checks["host_match"] or not checks["not_event_host"] or not checks["no_injection"]
    if risk == "R4" or critical_fail:
        return {"verdict": "DENY", "risk": risk, "checks": checks}
    if risk in ("R2", "R3"):
        return {"verdict": "NEED_HUMAN_APPROVAL", "risk": risk, "checks": checks}
    if not all(checks.values()):
        return {"verdict": "DENY", "risk": risk, "checks": checks}
    return {"verdict": "ALLOW", "risk": risk, "checks": checks}


def _legacy(artifact, action_proposal: dict) -> dict:
    """Legacy single-test path: evidence + risk only."""
    risk = classify_risk(action_proposal.get("name", ""))
    if risk == "R4":
        return {"verdict": "DENY", "risk": risk}
    if risk in ("R2", "R3") and not artifact.evidence_refs:
        return {"verdict": "DENY", "risk": risk, "reason": "evidence insufficient"}
    if risk == "R3":
        return {"verdict": "NEED_HUMAN_APPROVAL", "risk": risk}
    return {"verdict": "ALLOW", "risk": risk}
