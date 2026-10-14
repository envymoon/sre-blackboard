"""MCP tool-call gate tests (prototype; replaced by the tool layer in M3)."""

from app.mcp import check_tool_call, tool_server


def test_servers_and_rollback_gates():
    assert tool_server("elasticsearch.search_logs") == "Observability MCP"
    auth = {"tenant_id": "t1", "principal_id": "a"}
    assert check_tool_call(auth, "elasticsearch.search_logs", {"tenant_id": "t1"})["allow"]
    assert not check_tool_call(
        auth,
        "rollback_release",
        {
            "tenant_id": "t1",
            "incident_id": "i",
            "service": "s",
            "target_version": "v",
            "reason": "r",
            "idempotency_key": "k",
        },
    )["allow"]
    assert check_tool_call(
        auth,
        "rollback_release",
        {
            "tenant_id": "t1",
            "incident_id": "i",
            "service": "s",
            "target_version": "v",
            "reason": "r",
            "idempotency_key": "k",
        },
        approved=True,
    )["allow"]
    assert not check_tool_call({"tenant_id": "t1"}, "elasticsearch.search_logs", {"tenant_id": "t2"})["allow"]
