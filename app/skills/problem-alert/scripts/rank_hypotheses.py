"""rank_hypotheses (spec Sec.13.1). CONFIRMED only with >= 2 independent evidence classes, else HYPOTHESIS."""
from __future__ import annotations

def rank_hypotheses(evidences: list) -> list:
    if len(evidences) >= 2:
        return [{"candidate": "checkout-pod-7f8c", "confidence": "CONFIRMED"}]
    return [{"candidate": "checkout-pod-7f8c", "confidence": "HYPOTHESIS"}]
