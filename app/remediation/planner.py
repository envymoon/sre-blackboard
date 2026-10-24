"""The planner: the fifth agent. It reads the diagnosis and proposes a repair from the catalogue.

Same loop, checkpoint and citation check as the investigation (app/agents): it may look at
a Deployment's rollout history, a diff or its events for a step or two, and every action it
proposes must cite evidence it saw or the diagnosis cites. Proposing is all it can do; the
policy engine decides, a human approves, the executor acts (ADR-0007).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agents import prompts
from app.agents.citations import Checked, check_plan
from app.agents.investigation import IncidentView
from app.agents.loop import AgentFailed, AgentSpec, BudgetExceeded, Deps, run_agent
from app.agents.schemas import Diagnosis
from app.llm.types import LLMError
from app.remediation.plans import RemediationPlan

log = logging.getLogger(__name__)

PLANNER = "planner"
PLANNER_TOOLS = ("rollout_history", "deploy_diff", "k8s_events")


@dataclass(frozen=True)
class Proposal:
    plan: RemediationPlan | None
    error: str | None = None  # why there is no plan
    reused: bool = False  # read back from the checkpoint, the model was not asked again


async def propose(
    incident: IncidentView, diagnosis: Diagnosis, deps: Deps, *, model: str, max_steps: int = 3
) -> Proposal:
    """Raises LLMError only when retrying later may help (rate limit, outage)."""
    runs = await deps.store.runs(incident.id)
    done = runs.get(PLANNER)
    if done is not None and done.status == "done":
        return Proposal(RemediationPlan.model_validate(done.result["plan"]), reused=True)

    spec = AgentSpec(PLANNER, prompts.planner(), PLANNER_TOOLS, RemediationPlan, max_steps=max_steps)
    cited = diagnosis.cited()

    async def check(plan: RemediationPlan, shown: dict[str, str]) -> Checked[RemediationPlan]:
        return check_plan(plan, await deps.store.evidence_tools(incident.id, plan.cited()), set(shown) | cited)

    task = task_text(incident, diagnosis, await deps.store.evidence_tools(incident.id, cited))
    await deps.store.start_run(incident.tenant_id, incident.id, PLANNER, model, spec.prompt.version)
    try:
        run = await run_agent(spec, task, model=model, deps=deps, check=check)
    except BudgetExceeded as e:
        await deps.store.fail_run(incident.id, PLANNER, 0, "token budget exhausted")
        return Proposal(None, f"token budget exhausted ({e})")
    except LLMError as e:
        await deps.store.fail_run(incident.id, PLANNER, 0, str(e))
        if e.retryable and e.code != "invalid_output":
            raise
        return Proposal(None, f"the planner failed: {e}")
    except AgentFailed as e:
        await deps.store.fail_run(incident.id, PLANNER, 0, str(e))
        return Proposal(None, str(e))
    assert isinstance(run.final, RemediationPlan)
    await deps.store.finish_run(
        incident.tenant_id,
        incident.id,
        PLANNER,
        run.steps,
        {**run.summary(), "plan": run.final.model_dump(mode="json")},
        [],
    )
    return Proposal(run.final)


def task_text(incident: IncidentView, dx: Diagnosis, evidence_tools: dict[str, str]) -> str:
    def cites(ids: list[str]) -> str:
        return ", ".join(f"{ev} ({evidence_tools.get(ev, '?')})" for ev in ids)

    parts = [incident.describe(), "", f"Diagnosis: {dx.summary}", "Root cause candidates, most likely first:"]
    parts += [
        f"{i}. {rc.service} ({rc.confidence:.0%}): {rc.cause} [evidence: {cites(rc.evidence_ids)}]"
        for i, rc in enumerate(dx.root_causes, 1)
    ]
    if dx.facts:
        parts += ["Facts:", *(f"- {c.statement} [evidence: {cites(c.evidence_ids)}]" for c in dx.facts)]
    if dx.inferences:
        parts += ["Inferences:", *(f"- {c.statement} [evidence: {cites(c.evidence_ids)}]" for c in dx.inferences)]
    if dx.hypotheses:
        parts += ["Not verified:", *(f"- {h.statement}" for h in dx.hypotheses)]
    return "\n".join(parts)
