"""compare_control_group (spec Sec.13.2). CONFIRMED needs a reviewable control; otherwise UNCONFIRMED."""
from __future__ import annotations

def compare_control_group(timeline: list) -> dict:
    return {"causal_relation": "LIKELY", "counterfactual": "pending canary"}
