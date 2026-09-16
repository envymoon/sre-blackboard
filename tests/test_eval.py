"""Eval tests: 100-sample frozen replay + release gates. Maps to spec Sec.7.6/Sec.0.3."""
import json, pathlib
from app.eval import build_golden_100, run_eval


def test_eval_100_gates():
    samples = build_golden_100()
    assert len(samples) == 100
    assert {s["alert_type"] for s in samples} == {"Problem", "Business", "Event", "Host"}
    report = run_eval(samples)
    assert report["samples"] == 100 and report["config_version"] == "2.0.0"
    for k in ["routing_accuracy", "high_risk_recall", "false_pass", "cross_tenant",
              "double_claim", "schema_fail", "hitrate", "mrr", "overall_accuracy", "blocked"]:
        assert k in report, k
    assert not report["blocked"]  # a self-consistent synthetic set must pass; gate real releases on this
    pathlib.Path("eval/report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def test_eval_blocks_on_leak():
    from app.eval import replay_sample
    bad = {"sample_id": "G-X", "alert_type": "Problem", "priority": "P0",
           "alert": {"service_id": "checkout-service", "change_id": "c", "known_events": [1], "problem_hosts": ["p"]},
           "expected_plan": ["Problem", "Business", "Event", "Host"],
           "expected_evidence": ["E1"], "risk": "R0", "action": "read_metrics",
           "tenant": "t1", "expect_tenant_leak": True}
    assert replay_sample(bad)["cross_tenant"]
