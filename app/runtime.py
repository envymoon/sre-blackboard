"""Runtime: sandboxed execution environment. Maps to spec Sec.6.1.
Returns Observations only; never mutates Incident/Task state."""
from __future__ import annotations
import time


READ_ONLY_TOOLS = {
    "elasticsearch.search_logs",
    "prometheus.query_metrics",
    "trace.search",
    "cmdb.describe",
    "change.describe",
    "knowledge.search",
    "code.search_slice",
}


class AgentRuntime:
    def execute(self, action: dict, execution_context: dict) -> dict:
        t0 = time.time()
        name = action.get("name", "")
        if name not in READ_ONLY_TOOLS and action.get("type") != "finish":
            return {"status": "DENIED", "reason": f"tool not allowlisted: {name}"}
        # Minimal stub: returns an evidence reference; real ES/Prometheus backends plug in later
        tool_call_id = f"tool-{execution_context.get('task_id', 'x')}-{int(t0*1000)%100000}"
        evidence_id = f"EV-{tool_call_id[-5:]}"
        return {
            "status": "ok",
            "data_ref": evidence_id,
            "summary": f"{name} ok",
            "runtime_meta": {"sandbox_id": "local-stub", "duration_ms": int((time.time()-t0)*1000)},
            "tool_call_id": tool_call_id,
            "evidence_id": evidence_id,
        }
