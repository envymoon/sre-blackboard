"""Concurrency bounds. Maps to spec Sec.28.6: protect P0 on overload, pause P2/P3 dispatch, hold low priority in the ZSET."""
from __future__ import annotations

LIMITS = {"host_cpu_cores": 4, "host_memory_gb": 8, "supported_users": 100,
          "peak_http_requests": 20, "max_active_incidents": 5,
          "max_active_tasks_per_incident": 5, "api_workers": 2, "harness_workers": 2,
          "model_requests_in_flight": 4, "tool_requests_in_flight": 10}


def check_overload(active_incidents: int, model_in_flight: int, tool_in_flight: int) -> dict:
    actions = []
    if active_incidents > LIMITS["max_active_incidents"]:
        actions.append("pause_P2_P3_dispatch")
    if model_in_flight > LIMITS["model_requests_in_flight"]:
        actions.append("throttle_model_keep_P0")
    if tool_in_flight > LIMITS["tool_requests_in_flight"]:
        actions.append("throttle_tools_readonly_P0_only")
    return {"overload": bool(actions), "actions": actions or ["ok"]}
