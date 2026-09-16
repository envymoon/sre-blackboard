"""Conformance tests: skill inventory / experience tables / recovery / observability. Maps to spec Sec.13.0, Sec.11, Sec.20."""
import importlib.util
import pathlib
from app.persistence import Store
from app.observe import PERF_TARGETS, Monitor


def _load(path: str):
    spec = importlib.util.spec_from_file_location("m", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_skill_scripts_match_doc():
    base = pathlib.Path("app/skills")
    expect = {"problem-alert": ["build_window.py", "collect_problem_evidence.py", "rank_hypotheses.py"],
              "event-alert": ["build_event_timeline.py", "compare_control_group.py", "publish_causality.py"],
              "business-alert": ["load_business_baseline.py", "check_funnel.py", "verify_recovery.py"],
              "host-alert": ["load_host_snapshot.py", "select_peer_hosts.py", "correlate_service_impact.py"]}
    for skill, files in expect.items():
        for f in files:
            assert (base / skill / "scripts" / f).exists(), f
    assert _load("app/skills/problem-alert/scripts/rank_hypotheses.py").rank_hypotheses([{"a": 1}])[0]["confidence"] == "HYPOTHESIS"
    assert _load("app/skills/business-alert/scripts/verify_recovery.py").verify_recovery([1, 2]) == "business_recovered"


def test_experience_and_recovery():
    s = Store()
    s.save_incident("inc-r", "t1", "Problem", "P0", {})
    s.save_task("t-r", "inc-r", "t1", "problem-alert-skill", "P0", status="ARTIFACT_PUBLISHED")
    s.save_artifact("a-r", "inc-r", "t-r", "t1", {"artifact_type": "Evidence", "producer": "p"}, ["E1"])
    s.save_candidate("c-1", "inc-r", "t1", "checkout-service", {"root": "retry"})
    s.publish_experience("e-1", "t1", "checkout-service", {"root": "retry"}, "owner")
    st = s.load_incident_state("inc-r")
    assert "t-r" in st.tasks and len(st.artifacts) == 1


def test_observe_targets():
    assert PERF_TARGETS["ack_s"] == 1 and PERF_TARGETS["trace_complete"] == 1.0
    m = Monitor()
    m.incr("alerts")
    assert m.snapshot()["counters"]["alerts"] == 1
