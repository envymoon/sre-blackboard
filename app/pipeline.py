"""Fixed 10-stage workflow driver. Maps to spec Sec.8.5/Sec.13.5 FIXED_STAGES. Agents cannot create tasks."""
from __future__ import annotations
from .models import Task, IncidentState
from .harness import AgentHarness, FIXED_STAGES
from .agents import coordinator_plan, run_log_agent_once


def run_p0_problem_pipeline(alert: dict, harness: AgentHarness) -> IncidentState:
    incident_id = "inc-p0-smoke-001"
    state = IncidentState(incident_id=incident_id, stage=FIXED_STAGES[0])
    harness.store.save_state(state)

    # normalize_and_deduplicate -> verify_impact (minimal stub: pass through)
    state.stage = "coordinator_planning"
    plan = coordinator_plan(alert)  # Sec.13.5

    # initial_task_dispatch: build one task per plan entry
    state.stage = "initial_task_dispatch"
    cap_map = {"Problem": "logs", "Business": "metrics", "Event": "change", "Host": "host"}
    for i, alert_type in enumerate(plan):
        t = Task(task_id=f"t-{i+1}", incident_id=incident_id, alert_type=alert_type,
                 priority="P0", capability=cap_map[alert_type], service="checkout-service",
                 time_window={"lookback": "30m", "forward": "15m"})
        state.tasks[t.task_id] = t

    # dynamic_evidence_collection: run only the logs task here; the rest yield
    # NEED_MORE_EVIDENCE and are covered by later milestones
    state.stage = "dynamic_evidence_collection"
    for t in state.tasks.values():
        if t.capability == "logs":
            run_log_agent_once(t, harness)

    state.stage = "coordinator_synthesis"
    state.stage = "safety_review"
    harness.store.save_state(state)
    return state
