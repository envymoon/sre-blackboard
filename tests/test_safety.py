"""Safety review tests."""

from app.models import Artifact
from app.safety import safety_review


def test_safety_ten_checks():
    art = Artifact(
        incident_id="i",
        producer="p",
        artifact_type="t",
        facts=[],
        evidence_refs=["E1"],
        hypotheses=[],
        gaps=[],
        next_task_proposals=[],
    )
    ctx = {
        "target_host": "checkout-pod-7f8c",
        "problem_hosts": ["checkout-pod-7f8c"],
        "event_host": "prod-gw-03",
        "blast_radius": "1 deploy",
        "alternative_considered": True,
        "rollback_plan": True,
        "risk": "R3",
        "approval": True,
        "caller_perm": True,
        "args": {"service": "s"},
        "verify_plan": True,
    }
    r = safety_review(art, {"name": "rollback_release"}, ctx)
    assert r["verdict"] == "NEED_HUMAN_APPROVAL" and len(r["checks"]) == 10
    bad = safety_review(art, {"name": "rollback_release"}, {**ctx, "target_host": "prod-gw-03"})
    assert bad["verdict"] == "DENY"  # mistaking the Event host for the root cause is blocked outright
