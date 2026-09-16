"""build_window (spec Sec.13.1). P0 looks back 30m and ahead 15m."""
from __future__ import annotations

def build_window(alert: dict) -> dict:
    return {"lookback": "30m", "forward": "15m", "service": alert.get("service_id")}
