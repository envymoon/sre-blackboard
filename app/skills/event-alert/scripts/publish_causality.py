"""publish_causality (spec Sec.13.2). Publishes the causality verdict only; never executes remediation."""
from __future__ import annotations

def publish_causality(comparison: dict) -> dict:
    return {"causal_relation": comparison.get("causal_relation", "UNCONFIRMED")}
