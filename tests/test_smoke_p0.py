"""Smoke test: minimal P0 Problem closed loop. Maps to spec Sec.24.13 + Sec.27.2. Never touches index.md."""
from app.models import Task, IncidentState, Artifact
from app.harness import AgentHarness, InMemoryStore
from app.runtime import AgentRuntime
from app.agents import coordinator_plan, run_log_agent_once
from app.safety import safety_review


def test_p0_minimal_closed_loop():
    alert = {"service_id": "checkout-service", "environment": "production",
             "change_id": "chg-20260915-884", "known_events": [1], "problem_hosts": ["checkout-pod-7f8c-a"]}
    plan = coordinator_plan(alert)
    assert plan == ["Problem", "Business", "Event", "Host"]

    store = InMemoryStore()
    harness = AgentHarness(runtime=AgentRuntime(), store=store)
    state = IncidentState(incident_id="inc-p0-smoke-001")
    store.save_state(state)

    task = Task(task_id="t-log-1", incident_id=state.incident_id,
                alert_type="Problem", priority="P0", capability="logs",
                service="checkout-service", time_window={"lookback": "30m"})
    status = run_log_agent_once(task, harness)
    assert status == "ARTIFACT_PUBLISHED"
    assert len(state.artifacts) == 1

    verdict = safety_review(state.artifacts[0], {"name": "rollback_release"})
    assert verdict["verdict"] == "NEED_HUMAN_APPROVAL" and verdict["risk"] == "R3"
