"""Agent Loop: observe -> act -> verify -> converge. Maps to spec Sec.5.2.
Note: harness.execute_action is the control plane, not the tool itself."""
from __future__ import annotations


def run_agent_loop(task, context: dict, model, harness, max_steps: int = 5) -> str:
    for _step in range(max_steps):
        decision = model.decide(context)
        action = decision.get("action") if isinstance(decision, dict) else None
        if action is None:
            return "TASK_FAILED"
        if isinstance(action, dict) and action.get("type") == "finish":
            artifact = action.get("artifact")
            if artifact is not None and harness.validate_artifact(artifact):
                return "ARTIFACT_PUBLISHED"
            return "TASK_FAILED"
        result = harness.execute_action(task, action, context)
        if result.get("status") in ("DENIED", "BUDGET_EXCEEDED"):
            return "ESCALATE_TO_HUMAN"
        if result.get("status") == "ARTIFACT_PUBLISHED":
            return "ARTIFACT_PUBLISHED"
        context = harness.update_context(context, result)
    return "NEED_MORE_EVIDENCE"
