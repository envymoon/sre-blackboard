"""load_host_snapshot (spec Sec.13.4)."""
from __future__ import annotations

def load_host_snapshot(host_id: str) -> dict:
    return {"host_id": host_id, "cpu": "high", "restarts": 0}
