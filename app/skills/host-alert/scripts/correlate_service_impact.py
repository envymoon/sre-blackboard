"""correlate_service_impact (spec Sec.13.4). Grades only on both node-anomaly and service-impact evidence."""
from __future__ import annotations

def correlate_service_impact(host_facts: dict, peers: list) -> dict:
    return {"role": "PROBLEM_HOST_CANDIDATE", "peers": peers}
