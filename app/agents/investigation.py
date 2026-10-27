"""One incident's investigation: coordinator -> three investigators in parallel -> synthesizer.

(Or, for the evaluation's single-agent variant, one agent with every tool that writes the
diagnosis itself; app/agents/variants.py.)

Every agent run is checkpointed (agent_runs) and its output written to the blackboard in
the same transaction. When the job runs again after a crash or a lost lease, finished agents
are skipped and their output is read back, so no completed investigator is ever asked again;
an agent that was interrupted starts over (its tool calls reuse their evidence rows).

A failed investigator does not stop the others; the synthesizer works with what there is.
Running out of token budget, or ending with no root cause the evidence supports, is an
escalation to a human, not a guess.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.agents import prompts
from app.agents.citations import Checked, check_diagnosis, check_report
from app.agents.loop import AgentFailed, AgentRun, AgentSpec, BudgetExceeded, Deps, run_agent
from app.agents.schemas import INVESTIGATORS, Diagnosis, Finding, InvestigatorReport, Plan
from app.agents.store import Entry
from app.agents.variants import DEFAULT, Variant
from app.llm.types import LLMError
from app.tools.compress import iso

log = logging.getLogger(__name__)

INVESTIGATOR_TOOLS: dict[str, tuple[str, ...]] = {
    "metrics": ("prom_query", "prom_range"),
    "logs_traces": ("logs_search", "traces_search", "trace_get"),
    "changes": ("rollout_history", "deploy_diff", "k8s_events", "flag_state"),
}


@dataclass(frozen=True)
class IncidentView:
    """What the agents are told about the incident. Built from stored rows only (no clock), so
    a replayed run renders exactly the same text."""

    id: UUID
    tenant_id: str
    title: str
    service: str
    environment: str
    priority: str
    created_at: datetime
    labels: dict[str, str]
    annotations: dict[str, str] = field(default_factory=dict)
    # Other incidents open in the same environment around the same time.
    related: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            f"Incident: {self.title} on service {self.service} (priority {self.priority}, "
            f"environment {self.environment}).",
            f"Opened at {iso(self.created_at)}.",
            f"Alert labels: {json.dumps(self.labels, sort_keys=True)}",
        ]
        if self.annotations:
            lines.append(f"Alert annotations: {json.dumps(self.annotations, sort_keys=True)}")
        if self.related:
            lines.append("Other open incidents in this environment:")
            lines += [
                f"- {r['title']} on {r['service']} ({r['priority']}), opened {iso(r['created_at'])}"
                for r in self.related
            ]
        else:
            lines.append("No other open incidents in this environment.")
        return "\n".join(lines)


@dataclass(frozen=True)
class AgentConfig:
    model_for: Callable[[str], str]
    max_steps: int = 8
    variant: Variant = DEFAULT

    def model(self, role: str) -> str:
        return self.variant.models.get(role) or self.model_for(role)


@dataclass
class Outcome:
    diagnosis: Diagnosis | None = None
    escalate: str | None = None  # reason, when the incident should go to a human
    agents: dict[str, str] = field(default_factory=dict)  # agent -> done | failed | skipped (reused)


DEFAULT_BRIEFS = {
    "metrics": "Find which services have elevated errors or latency, and which went bad first.",
    "logs_traces": "Find the error messages and the failing or slow spans in the alerting service and its dependencies.",
    "changes": "Find rollouts, configuration changes, flag changes or pod problems shortly before the alert.",
}


CheckFn = Callable[[Any, dict[str, str]], Awaitable[Checked[Any]]]
ToEntries = Callable[[Any, AgentRun], tuple[list[Entry], dict[str, Any]]]
# Runs one agent unless a checkpoint says it already finished; its blackboard entries, or None if it failed.
Once = Callable[[AgentSpec, str, CheckFn, ToEntries], Awaitable[list[Entry] | None]]


async def investigate(incident: IncidentView, deps: Deps, config: AgentConfig) -> Outcome:
    outcome = Outcome()
    runs = await deps.store.runs(incident.id)
    board = await deps.store.entries(incident.id)

    async def once(spec: AgentSpec, task: str, check: CheckFn, to_entries: ToEntries) -> list[Entry] | None:
        if spec.name in runs and runs[spec.name].status == "done":
            outcome.agents[spec.name] = "reused"
            return [e for e in board if e.author == spec.name]
        model = config.model(spec.name)
        await deps.store.start_run(incident.tenant_id, incident.id, spec.name, model, spec.prompt.version)
        try:
            run = await run_agent(spec, task, model=model, deps=deps, check=check)
        except BudgetExceeded:
            await deps.store.fail_run(incident.id, spec.name, 0, "token budget exhausted")
            raise
        except LLMError as e:
            await deps.store.fail_run(incident.id, spec.name, 0, str(e))
            if e.retryable and e.code != "invalid_output":
                raise  # rate limit or outage: the whole job retries later, with backoff
            outcome.agents[spec.name] = "failed"
            log.warning("%s failed: %s", spec.name, e)
            return None
        except AgentFailed as e:
            await deps.store.fail_run(incident.id, spec.name, 0, str(e))
            outcome.agents[spec.name] = "failed"
            log.warning("%s", e)
            return None
        entries, extra = to_entries(run.final, run)
        await deps.store.finish_run(
            incident.tenant_id, incident.id, spec.name, run.steps, {**run.summary(), **extra}, entries
        )
        outcome.agents[spec.name] = "done"
        return entries

    try:
        if config.variant.mode == "single":
            dx_entries = await _solo(incident, deps, config, once)
            return _concluded(outcome, dx_entries)
        # 1. Plan.
        plan_entries = await once(
            AgentSpec("coordinator", prompts.coordinator(), (), Plan, max_steps=1),
            incident.describe(),
            _accept,
            lambda plan, _: ([Entry("plan", "coordinator", plan.model_dump())], {}),
        )
        plan = Plan.model_validate(plan_entries[0].body) if plan_entries else None

        # 2. Investigate, in parallel.
        async def investigator(focus: str) -> list[Entry] | None:
            spec = AgentSpec(
                f"investigator.{focus}",
                prompts.investigator(focus),
                INVESTIGATOR_TOOLS[focus],
                InvestigatorReport,
                max_steps=config.max_steps,
            )

            async def check(report: InvestigatorReport, shown: dict[str, str]) -> Checked[InvestigatorReport]:
                cited = {ev for f in report.findings for ev in f.evidence_ids}
                return check_report(report, await deps.store.evidence_tools(incident.id, cited), shown)

            def entries(report: InvestigatorReport, run: AgentRun) -> tuple[list[Entry], dict[str, Any]]:
                return (
                    [Entry("finding", spec.name, f.model_dump(), list(f.evidence_ids)) for f in report.findings],
                    {"report_summary": report.summary},
                )

            return await once(spec, _investigator_task(incident, plan, focus), check, entries)

        # A TaskGroup, not gather: when one investigator hits the budget or a rate limit, the
        # others are cancelled instead of running on unattended. Finished ones stay finished.
        try:
            async with asyncio.TaskGroup() as group:
                for focus in INVESTIGATORS:
                    group.create_task(investigator(focus))
        except ExceptionGroup as eg:
            raise eg.exceptions[0] from None
        runs = await deps.store.runs(incident.id)
        board = await deps.store.entries(incident.id)
        findings = [(e.author, Finding.model_validate(e.body)) for e in board if e.kind == "finding"]
        if not findings:
            outcome.escalate = "the investigators found nothing they could support with evidence"
            return outcome

        # 3. Synthesize.
        cited = {ev for _, f in findings for ev in f.evidence_ids}
        evidence_tools = await deps.store.evidence_tools(incident.id, cited)

        async def check_dx(dx: Diagnosis, _: dict[str, str]) -> Checked[Diagnosis]:
            return check_diagnosis(dx, await deps.store.evidence_tools(incident.id, dx.cited()), cited)

        summaries = {name: run.result.get("report_summary", "") for name, run in runs.items()}
        dx_entries = await once(
            AgentSpec(
                "synthesizer", config.variant.prompt("synthesizer", prompts.synthesizer), (), Diagnosis, max_steps=1
            ),
            _synthesizer_task(incident, plan, findings, summaries, evidence_tools),
            check_dx,
            lambda dx, _: ([Entry("diagnosis", "synthesizer", dx.model_dump(), sorted(dx.cited()))], {}),
        )
    except BudgetExceeded as e:
        outcome.escalate = f"token budget exhausted ({e})"
        return outcome

    return _concluded(outcome, dx_entries)


def _concluded(outcome: Outcome, dx_entries: list[Entry] | None) -> Outcome:
    if not dx_entries:
        outcome.escalate = "the synthesizer could not produce a diagnosis"
        return outcome
    outcome.diagnosis = Diagnosis.model_validate(dx_entries[0].body)
    if not outcome.diagnosis.root_causes:
        outcome.escalate = "no root cause candidate is supported by the evidence"
    return outcome


SOLO = "solo"


async def _solo(incident: IncidentView, deps: Deps, config: AgentConfig, once: Once) -> list[Entry] | None:
    """The single-agent variant: every investigator's tools and their steps together, and
    the synthesizer's answer at the end. Its citations may name anything it was shown."""
    tools = tuple(t for focus in INVESTIGATORS for t in INVESTIGATOR_TOOLS[focus])
    spec = AgentSpec(SOLO, prompts.solo(), tools, Diagnosis, max_steps=config.max_steps * len(INVESTIGATORS))

    async def check(dx: Diagnosis, shown: dict[str, str]) -> Checked[Diagnosis]:
        return check_diagnosis(dx, await deps.store.evidence_tools(incident.id, dx.cited()), shown)

    return await once(
        spec,
        incident.describe(),
        check,
        lambda dx, _: ([Entry("diagnosis", SOLO, dx.model_dump(), sorted(dx.cited()))], {}),
    )


async def _accept(value: Any, _: dict[str, str]) -> Checked[Any]:
    return Checked(value, [], 0)


def _plan_text(plan: Plan | None) -> str:
    if plan is None:
        return "The coordinator did not produce a plan."
    hypotheses = "\n".join(f"{i}. {h}" for i, h in enumerate(plan.hypotheses, 1)) or "(none)"
    return f"Coordinator's summary: {plan.summary}\nHypotheses to check:\n{hypotheses}"


def _investigator_task(incident: IncidentView, plan: Plan | None, focus: str) -> str:
    brief = getattr(plan.briefs, focus) if plan else DEFAULT_BRIEFS[focus]
    return f"{incident.describe()}\n\n{_plan_text(plan)}\n\nYour brief: {brief}"


def _synthesizer_task(
    incident: IncidentView,
    plan: Plan | None,
    findings: list[tuple[str, Finding]],
    summaries: dict[str, str],
    evidence_tools: dict[str, str],
) -> str:
    parts = [incident.describe(), _plan_text(plan), "Findings:"]
    for author in sorted({a for a, _ in findings}):
        parts.append(f"\n[{author}] {summaries.get(author, '')}")
        for f in (f for a, f in findings if a == author):
            cites = ", ".join(f"{ev} ({evidence_tools.get(ev, '?')})" for ev in f.evidence_ids) or "none"
            about = f", service {f.service}" if f.service else ""
            parts.append(
                f"- ({f.kind}, {f.signal}{about}, confidence {f.confidence:.2f}) {f.statement} [evidence: {cites}]"
            )
    return "\n".join(parts)
