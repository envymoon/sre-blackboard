"""load_business_baseline (spec Sec.13.3)."""
from __future__ import annotations

def load_business_baseline(service: str) -> dict:
    return {"service": service, "baseline_window": "7d", "data_freshness": "ok"}
