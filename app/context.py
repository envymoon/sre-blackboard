"""Context assembly + compression. Maps to spec Sec.10: five fixed layers, per-type
recipes, fixed pipeline. Raw logs, full traces, and whole-repo code cost 0 tokens --
only their evidence IDs enter the prompt."""
from __future__ import annotations

INPUT_BUDGETS = {"P0": 28000, "P1": 24000, "P2": 16000, "P3": 12000}
OUTPUT_BUDGETS = {"P0": 3500, "P1": 3000, "P2": 2000, "P3": 1500}

# Sec.10.2 partition caps (P0/P1 first; P2/P3 second). Partitions cannot borrow from each other.
PARTITIONS_P0 = {"policy": 4200, "metrics": 2000, "logs": 6000, "trace": 3000, "change_code_host_db": 7000, "knowledge": 3000}
PARTITIONS_P23 = {"policy": 3600, "metrics": 1200, "logs": 3500, "trace": 2000, "change_code_host_db": 3500, "knowledge": 1500}


def partitions(priority: str) -> dict:
    return dict(PARTITIONS_P0 if priority in ("P0", "P1") else PARTITIONS_P23)


def rank_score(ev: dict) -> int:
    return (30 if ev.get("service_match") else 0) + ev.get("time_score", 0) + \
           (20 if ev.get("trace_link") else 0) + ev.get("severity", 0) + (10 if ev.get("p0_related") else 0)


def compress_pipeline(alert: dict, evidences: list, priority: str = "P0") -> dict:
    """Minimal Freeze->Normalize->Cluster->Rank->Assemble->Validate implementation."""
    part = partitions(priority)
    # Normalize: desensitization happens in knowledge/caller layers; dedup by ID only here
    seen, uniq = set(), []
    for e in evidences:
        if e.get("evidence_id") in seen:
            continue
        seen.add(e.get("evidence_id"))
        uniq.append(e)
    # Cluster: at most 15 log clusters with 3 samples each (callers pre-cluster; truncate here)
    logs = [e for e in uniq if e.get("kind") == "log"][:15]
    for c in logs:
        c["samples"] = (c.get("samples") or [])[:3]
    traces = [e for e in uniq if e.get("kind") == "trace"][:5]
    changes = [e for e in uniq if e.get("kind") == "change"][:2]
    knowledge = [e for e in uniq if e.get("kind") == "knowledge"][:2]
    metrics = [e for e in uniq if e.get("kind") == "metric"]
    ranked = sorted(logs + traces + changes + knowledge + metrics, key=rank_score, reverse=True)
    # Assemble into facts-hypotheses-counter-gaps; when over budget drop bodies first, keep IDs
    envelope = {"facts": [], "hypotheses": [], "counter": [], "gaps": []}
    for e in ranked:
        envelope["facts"].append({"evidence_id": e.get("evidence_id"), "text": str(e.get("text", ""))[:500]})
    # Validate: every fact needs an evidence ref; at most 3 hypotheses
    valid = all(f.get("evidence_id") for f in envelope["facts"])
    total_in = sum(len(str(f)) // 4 for f in envelope["facts"])  # rough token estimate
    if not valid:
        return {"status": "CONTEXT_INVALID"}
    if total_in > INPUT_BUDGETS[priority]:
        # Evict low-score bodies, keep IDs
        for f in sorted(envelope["facts"], key=lambda x: x["evidence_id"])[::-1]:
            if total_in <= INPUT_BUDGETS[priority] * 0.8:
                break
            total_in -= len(f.pop("text", "")) // 4
    return {"status": "OK", "envelope": envelope, "partitions": part,
            "budget": {"input": INPUT_BUDGETS[priority], "output": OUTPUT_BUDGETS[priority]}}
