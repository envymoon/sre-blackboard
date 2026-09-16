"""Gap-closure tests. Maps to spec Sec.13.0.1, Sec.17.3, Sec.13.8, Sec.21.2."""
from app.code_analysis import analyze, LIMITS
from app.safety import safety_review
from app.evolution import attribute_gap, propose_candidate, static_validate, publish_gate
from app.online import OnlineMetrics, check_p0_sla
from app.models import Artifact


def test_code_analysis_gates():
    assert analyze()["status"] == "NOT_APPLICABLE"
    assert analyze(change_id="chg-1", failures=2)["status"] == "CODE_EVIDENCE_UNAVAILABLE"
    ok = analyze(change_id="chg-1", symbol="f")
    assert ok["status"] == "OK" and ok["shell"] == "disabled"
    assert LIMITS["files"] == 12 and LIMITS["input"] == 18000


def test_safety_ten_checks():
    art = Artifact(incident_id="i", producer="p", artifact_type="t", facts=[],
                   evidence_refs=["E1"], hypotheses=[], gaps=[], next_task_proposals=[])
    ctx = {"target_host": "checkout-pod-7f8c", "problem_hosts": ["checkout-pod-7f8c"],
           "event_host": "prod-gw-03", "blast_radius": "1 deploy", "alternative_considered": True,
           "rollback_plan": True, "risk": "R3", "approval": True, "caller_perm": True,
           "args": {"service": "s"}, "verify_plan": True}
    r = safety_review(art, {"name": "rollback_release"}, ctx)
    assert r["verdict"] == "NEED_HUMAN_APPROVAL" and len(r["checks"]) == 10
    bad = safety_review(art, {"name": "rollback_release"}, {**ctx, "target_host": "prod-gw-03"})
    assert bad["verdict"] == "DENY"  # mistaking the Event host for the root cause is blocked outright


def test_evolution_and_online():
    assert attribute_gap({"gap": "wrong_rank"}) == "wrong_rank"
    assert propose_candidate(["x"])["status"] == "NEED_MORE_SAMPLES"
    cand = propose_candidate(["wrong_rank"] * 5)
    assert static_validate(cand)["status"] == "PASS"
    assert publish_gate({"overall_accuracy": 0.9}, {"overall_accuracy": 0.92, "false_pass": 0})["decision"] == "PROMOTE"
    assert publish_gate({"overall_accuracy": 0.9}, {"overall_accuracy": 0.9, "false_pass": 1})["decision"] == "ROLLBACK"
    m = OnlineMetrics()
    m.record(20, 300, True, True, 0.12)
    assert m.summary()["adoption"] == 1.0
    assert check_p0_sla(25, 80) == {"start_ok": True, "evidence_ok": True}
