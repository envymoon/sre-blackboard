"""Harness: the control plane. Maps to spec Sec.7.4. Never executes tools directly;
it only orchestrates the Loop and the Runtime."""
from __future__ import annotations
import time
from typing import Any, Dict, List
from .models import Task, Artifact, IncidentState


FIXED_STAGES = [
    "normalize_and_deduplicate",
    "verify_impact",
    "coordinator_planning",
    "initial_task_dispatch",
    "dynamic_evidence_collection",
    "coordinator_synthesis",
    "safety_review",
    "human_approval_if_required",
    "recovery_verification",
    "resolve_or_escalate",
]


class InMemoryStore:
    def __init__(self):
        self.states: Dict[str, IncidentState] = {}
        self.artifacts: List[Artifact] = []
        self.checkpoints: Dict[str, dict] = {}

    def save_state(self, s: IncidentState):
        self.states[s.incident_id] = s

    def save_artifact(self, a: Artifact):
        self.artifacts.append(a)

    def save_checkpoint(self, key: str, payload: dict):
        self.checkpoints[key] = payload


class Tracer:
    def __init__(self):
        self.events: List[dict] = []

    def append(self, kind: str, payload: dict):
        self.events.append({"kind": kind, **payload})  # type: ignore


class AgentHarness:
    """Sec.7.4 run_task / validate_action / validate_artifact / save_checkpoint / apply_observation."""

    def __init__(self, runtime, store=None, tracer=None):
        self.runtime = runtime
        self.store = store or InMemoryStore()
        self.tracer = tracer or Tracer()

    def build_context(self, task: Task, state: IncidentState) -> dict:
        return {
            "incident_id": task.incident_id,
            "task_id": task.task_id,
            "alert_type": task.alert_type,
            "priority": task.priority,
            "service": task.service,
            "context_version": "ctx-v1",
        }

    def validate_action(self, action: dict) -> bool:
        # Minimal check: must carry type/name; the read-only allowlist is enforced again by the Runtime
        return isinstance(action, dict) and "type" in action and "name" in action

    def validate_artifact(self, artifact: Artifact) -> bool:
        return artifact.validate()

    def save_checkpoint(self, key: str, payload: dict):
        self.store.save_checkpoint(key, payload)

    def apply_observation(self, context: dict, observation: dict) -> dict:
        context["last_observation"] = observation
        return context

    def update_context(self, context: dict, observation: dict) -> dict:
        return self.apply_observation(context, observation)

    def try_claim(self, task: Task, agent_id: str, ttl_s: int = 90) -> bool:
        if task.status != "READY":
            return False
        task.status = "CLAIMED"
        task.lease_until = time.time() + ttl_s
        self.save_checkpoint(f"{task.incident_id}+{task.task_id}+claim", {"agent_id": agent_id})
        return True

    def publish_artifact(self, task: Task, artifact: Artifact, state: IncidentState):
        assert self.validate_artifact(artifact), "artifact schema invalid"
        task.status = "ARTIFACT_PUBLISHED"
        state.artifacts.append(artifact)
        self.store.save_artifact(artifact)
        self.save_checkpoint(
            f"{task.incident_id}+{task.task_id}+artifact",
            {"artifact_type": artifact.artifact_type, "evidence": artifact.evidence_refs},
        )
        self.tracer.append("ARTIFACT_PUBLISHED", {"task_id": task.task_id})

    def execute_action(self, task: Task, action: dict, context: dict) -> dict:
        if not self.validate_action(action):
            return {"status": "DENIED"}
        return self.runtime.execute(action, {"task_id": task.task_id})

    def run_task(self, task: Task, state: IncidentState, model, max_steps: int = 5,
                 gateway=None, input_tokens: int = 2000, output_tokens: int = 500) -> str:
        from .loop import run_agent_loop
        mid = getattr(model, "model_id", "unknown")
        tier = getattr(model, "tier", getattr(model, "thinking_level", ""))
        self.tracer.append("MODEL_DECISION", {"task_id": task.task_id, "model_id": mid, "tier": tier})
        if gateway is not None:
            agent = getattr(model, "agent_role", "log")
            g = gateway.complete(agent, input_tokens, output_tokens)
            if g.get("status") == "BUDGET_EXCEEDED":
                return "ESCALATE_TO_HUMAN"
        return run_agent_loop(task, self.build_context(task, state), model, self, max_steps)
