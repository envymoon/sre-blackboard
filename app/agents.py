"""Role agents + Coordinator. Maps to spec Sec.9.1.2, Sec.13.5, Sec.9.1.
Agents only emit next_task_proposals; the Coordinator owns task creation."""
from __future__ import annotations
from .models import Task, Artifact


class StubModel:
    """Dual-model routing placeholder: primary=muse-spark-1.3 / fast=gemini-3.8-flash, picked by the Harness per role."""
    def __init__(self, model_id: str):
        self.model_id = model_id

    def decide(self, context: dict) -> dict:
        return {"action": {"type": "finish", "artifact": None}}


class LogAgent:
    name = "log-agent"
    capability = "logs"
    max_tool_calls = 5

    def can_handle(self, task: Task) -> bool:
        return task.capability == self.capability

    def plan_action(self, task: Task) -> dict:
        return {"type": "tool", "name": "elasticsearch.search_logs",
                "args": {"service": task.service, "window": task.time_window}}

    def build_artifact(self, task: Task, observation: dict) -> Artifact:
        ev = observation.get("evidence_id", "LOG-7782")
        return Artifact(
            incident_id=task.incident_id, producer=self.name, artifact_type="LogEvidence",
            facts=[{"statement": "Checkout TimeoutException spike", "evidence_ids": [ev]}],
            evidence_refs=[ev], hypotheses=[],
            gaps=[], next_task_proposals=[{"capability": "trace"}],
        )


def run_log_agent_once(task: Task, harness) -> str:
    agent = LogAgent()
    assert agent.can_handle(task)
    assert harness.try_claim(task, agent.name)
    task.status = "RUNNING"
    obs = harness.execute_action(task, agent.plan_action(task), {})
    artifact = agent.build_artifact(task, obs)
    # Minimal demo path: the caller owns the state, so resolve it from the harness store here.
    from .models import IncidentState
    state = harness.store.states.get(task.incident_id) or IncidentState(incident_id=task.incident_id)
    harness.store.save_state(state)
    harness.publish_artifact(task, artifact, state)
    return task.status


def coordinator_plan(alert: dict):
    """Sec.13.5 minimal rule: a P0 Problem always spawns Problem+Business; add Event when a release exists; add Host on anomalous Pods."""
    tasks = ["Problem", "Business"]
    if alert.get("change_id") or alert.get("known_events"):
        tasks.append("Event")
    if alert.get("problem_hosts"):
        tasks.append("Host")
    return tasks


def synthesize(artifacts: list):
    """Sec.13.5 synthesis: the artifact with most evidence wins; conflicts keep both sides' evidence plus follow-up tasks, never averaged."""
    best = max(artifacts, key=lambda a: len(a.evidence_refs))
    gaps = list(best.gaps)
    for a in artifacts:
        if a is not best:
            gaps.append(f"recheck {a.artifact_type} vs {best.artifact_type}")
    return best, gaps
