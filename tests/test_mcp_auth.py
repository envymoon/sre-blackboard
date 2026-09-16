"""MCP + security tests. Maps to spec Sec.14, Sec.17.1, Sec.26."""
import time
from app.mcp import check_tool_call, tool_server
from app.auth import ingress, runtime_claim_check, Approvals, AuditLog


def test_servers_and_rollback_gates():
    assert tool_server("elasticsearch.search_logs") == "Observability MCP"
    auth = {"tenant_id": "t1", "principal_id": "a"}
    assert check_tool_call(auth, "elasticsearch.search_logs", {"tenant_id": "t1"})["allow"]
    assert not check_tool_call(auth, "rollback_release",
        {"tenant_id": "t1", "incident_id": "i", "service": "s", "target_version": "v", "reason": "r", "idempotency_key": "k"})["allow"]
    assert check_tool_call(auth, "rollback_release",
        {"tenant_id": "t1", "incident_id": "i", "service": "s", "target_version": "v", "reason": "r", "idempotency_key": "k"},
        approved=True)["allow"]
    assert not check_tool_call({"tenant_id": "t1"}, "elasticsearch.search_logs", {"tenant_id": "t2"})["allow"]


def test_four_gates_and_approval_expiry():
    auth = {"tenant_id": "t1", "principal_id": "u1", "role_ids": ["oncall"],
            "service_scope": ["checkout-service"], "expires_at": time.time() + 60}
    assert ingress(auth)
    assert runtime_claim_check(auth, {"tenant_id": "t1", "service": "checkout-service"})
    assert not runtime_claim_check(auth, {"tenant_id": "t2", "service": "checkout-service"})
    ap = Approvals()
    ap.create_card("inc-1", "AP-1", "P0", "checkout-service", "2026.09.15.3", "2026.09.14.9", ["E1"], "low", expires_s=3600)
    assert ap.approve("inc-1", "AP-1", "owner", "k1")["approved"]
    ap2 = Approvals()
    ap2.create_card("inc-2", "AP-2", "P0", "s", "a", "b", [], "", expires_s=-1)
    assert not ap2.approve("inc-2", "AP-2", "owner", "k")["approved"]
    log = AuditLog()
    e1 = log.append("t1", "u1", "rollback_release", "checkout-service", "approved", "run-1")
    e2 = log.append("t1", "agent", "read", "logs", "allow", "run-1")
    assert e2["prev"] == e1["hash"]
