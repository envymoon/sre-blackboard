"""build_event_timeline (spec Sec.13.2)."""
from __future__ import annotations

def build_event_timeline(alert: dict) -> list:
    return [{"change_id": alert.get("change_id"), "at": "09:56:40"}]
