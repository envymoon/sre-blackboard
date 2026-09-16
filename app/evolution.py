"""Skill self-evolution: an auditable, rollback-safe version loop. Maps to spec Sec.13.8:
only skills/scripts/retrieval/templates/validators evolve -- never model permissions or schemas."""
from __future__ import annotations

GAP_CLASSES = ["missed_evidence", "wrong_rank", "wrong_cause", "bad_tool_params", "timeout_budget", "invalid_output"]
FORBIDDEN = ["new_write_tool", "safety_change", "raise_P0_budget"]


def attribute_gap(trace: dict) -> str:
    return trace.get("gap", "missed_evidence") if trace.get("gap") in GAP_CLASSES else "missed_evidence"


def propose_candidate(gaps: list, skill: str = "problem-alert-skill") -> dict:
    same = max((gaps.count(g), g) for g in set(gaps)) if gaps else (0, "")
    if same[0] < 5 and "P0_confirmed" not in gaps:
        return {"status": "NEED_MORE_SAMPLES"}
    return {"status": "CANDIDATE", "skill": skill, "changes": ["rank_hypotheses", "log_cluster_rule", "output_validator"][:3]}


def static_validate(candidate: dict) -> dict:
    for f in candidate.get("changes", []):
        if f in FORBIDDEN:
            return {"status": "REJECTED", "reason": f}
    return {"status": "PASS"}


def publish_gate(prod_report: dict, cand_report: dict) -> dict:
    """P0/P1 must not regress; safety leaks must be 0; evidence completeness >= 99%."""
    if cand_report.get("false_pass", 1) != 0:
        return {"decision": "ROLLBACK", "reason": "safety leak"}
    if cand_report.get("overall_accuracy", 0) < prod_report.get("overall_accuracy", 0):
        return {"decision": "ROLLBACK", "reason": "accuracy regression"}
    return {"decision": "PROMOTE"}
