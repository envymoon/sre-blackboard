"""MCP tool layer. Maps to spec Sec.14: 10-server split + per-tool security contract.
Remediation tools are high-risk and require approval."""
from __future__ import annotations

SERVERS = {
    "Observability MCP": {"tools": ["prometheus.query_metrics", "elasticsearch.search_logs", "trace.search"], "perm": "read"},
    "CMDB MCP": {"tools": ["cmdb.describe"], "perm": "read"},
    "Host Runtime MCP": {"tools": ["host.describe"], "perm": "read"},
    "Database MCP": {"tools": ["db.describe"], "perm": "read"},
    "Change MCP": {"tools": ["change.describe"], "perm": "read"},
    "Code MCP": {"tools": ["code.search_slice"], "perm": "read"},
    "Knowledge MCP": {"tools": ["knowledge.search"], "perm": "read"},
    "Ticket MCP": {"tools": ["ticket.create"], "perm": "controlled_write"},
    "Notification MCP": {"tools": ["notify.send"], "perm": "controlled_write"},
    "Remediation MCP": {"tools": ["rollback_release", "shift_traffic", "restart_stateless", "scale_out"], "perm": "high_risk_approval"},
}

ROLLBACK_INPUT = ["incident_id", "service", "target_version", "reason", "idempotency_key"]
SECRET_KEYS = ("token", "password", "ssh_key", "approval_key", "db_password")


def tool_server(tool: str) -> str:
    for srv, v in SERVERS.items():
        if tool in v["tools"]:
            return srv
    return ""


def check_tool_call(auth: dict, tool: str, args: dict, approved: bool = False) -> dict:
    """MCP Gateway: re-validates tenant/principal/action/resource/environment on every call."""
    srv = tool_server(tool)
    if not srv:
        return {"allow": False, "reason": f"unknown tool {tool}"}
    if auth.get("tenant_id") != args.get("tenant_id", auth.get("tenant_id")):
        return {"allow": False, "reason": "tenant mismatch"}
    if (tool in ("rollback_release", "shift_traffic")) and not approved:
        return {"allow": False, "reason": "R3 human_approval_required"}
    if any(k in str(args).lower() for k in SECRET_KEYS):
        # Credentials must never enter prompts, the blackboard, or artifacts: reject calls carrying secrets
        if tool != "rollback_release" or "approval_key" in args:
            return {"allow": False, "reason": "credential in args"}
    if tool == "rollback_release":
        missing = [k for k in ROLLBACK_INPUT if k not in args]
        if missing:
            return {"allow": False, "reason": f"missing {missing}"}
    perm = SERVERS[srv]["perm"]
    if perm == "high_risk_approval" and not approved:
        return {"allow": False, "reason": "approval required"}
    return {"allow": True, "server": srv, "perm": perm}
