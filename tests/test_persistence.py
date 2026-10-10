"""Persistence tests: experience tables and blackboard recovery."""

from app.persistence import Store


def test_experience_and_recovery():
    s = Store()
    s.save_incident("inc-r", "t1", "Problem", "P0", {})
    s.save_task("t-r", "inc-r", "t1", "problem-alert-skill", "P0", status="ARTIFACT_PUBLISHED")
    s.save_artifact("a-r", "inc-r", "t-r", "t1", {"artifact_type": "Evidence", "producer": "p"}, ["E1"])
    s.save_candidate("c-1", "inc-r", "t1", "checkout-service", {"root": "retry"})
    s.publish_experience("e-1", "t1", "checkout-service", {"root": "retry"}, "owner")
    st = s.load_incident_state("inc-r")
    assert "t-r" in st.tasks and len(st.artifacts) == 1
