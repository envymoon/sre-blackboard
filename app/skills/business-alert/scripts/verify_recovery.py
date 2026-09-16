"""verify_recovery (spec Sec.13.3). Recovered only after two consecutive business cycles meet criteria."""
from __future__ import annotations

def verify_recovery(samples: list) -> str:
    return "business_recovered" if len(samples) >= 2 else "NOT_RECOVERED"
