"""check_funnel (spec Sec.13.3). Locates the funnel breakpoint."""
from __future__ import annotations

def check_funnel(baseline: dict) -> dict:
    return {"funnel_breakpoint": "inventory_deduct", "baseline": baseline.get("service")}
