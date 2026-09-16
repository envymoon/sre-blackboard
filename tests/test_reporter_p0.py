"""Reporter test: all 10 Sec.18 sections + evidence/approval/execution aligned with Sec.27."""
from app.reporter import build_report, render_markdown


def test_reporter_ten_sections():
    alert = {"alert_id": "ALERT-20260915-0001", "priority": "P0", "service": "checkout-service"}
    r = build_report(alert)
    for k in ["summary", "priority", "timeline", "hosts", "root_cause_candidates",
              "evidences", "completed_actions", "recommended_action",
              "facts_confirmed", "run_meta"]:
        assert k in r, k
    ids = {e["id"] for e in r["evidences"]}
    assert {"METRIC-1001", "LOG-7782", "TRACE-3304", "CHANGE-2201", "CODE-4402", "KB-0901"} <= ids
    assert r["recommended_action"]["risk_level"] == "R3"
    assert r["recommended_action"]["approval"]["decision"] == "approved"
    assert r["recommended_action"]["execution"]["execution_id"] == "ROLLBACK-20260915-001"
    assert r["root_cause_candidates"][0]["confidence"] == "HIGH_CONFIDENCE_CANDIDATE"
    md = render_markdown(r)
    assert "Undetermined" in md and "checkout-pod-7f8c" in md and "prod-gw-03" in md
