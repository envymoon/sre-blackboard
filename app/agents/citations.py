"""The citation check: deterministic, no model involved.

Every evidence id an agent cites must

1. exist and belong to this incident (looked up in the evidence store),
2. have been shown to that agent: an investigator may only cite results of its own queries,
   the synthesizer only evidence the accepted findings cite,
3. match the claim: a finding about logs must cite at least one log query.

Facts, inferences and root causes need at least one citation; a hypothesis may have none.
A knowledge base result (kb_search, M6) is background, not a measurement: it may be cited
next to live evidence, never alone (except by a hypothesis, which needs no evidence at all).
A failing answer goes back to the agent once with the list of problems; whatever still fails
after that is dropped, and the count is kept with the run.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

from app.agents.schemas import Diagnosis, Finding, InvestigatorReport
from app.remediation.plans import RemediationPlan

SIGNAL_TOOLS: dict[str, frozenset[str]] = {
    "metrics": frozenset({"prom_query", "prom_range"}),
    "logs": frozenset({"logs_search"}),
    "traces": frozenset({"traces_search", "trace_get"}),
    "changes": frozenset({"rollout_history", "deploy_diff", "k8s_events"}),
    "flags": frozenset({"flag_state"}),
}
# Runbooks and postmortems: what usually happens. Never the only support for a claim.
KNOWLEDGE_TOOLS = frozenset({"kb_search"})
KNOWLEDGE_ONLY = (
    "cites only knowledge base results; a runbook says what usually happens, not what happened now. "
    "Cite a live result too, or call it a hypothesis"
)


@dataclass(frozen=True)
class Checked[T]:
    value: T  # the answer with every failing item removed
    problems: list[str]  # empty: the answer passed as it was
    dropped: int


def _id_problems(ids: Collection[str], known: Mapping[str, str], shown: Collection[str], where: str) -> list[str]:
    out = []
    for ev in ids:
        if ev not in known:
            out.append(f"{where}: {ev} is not an evidence id of this incident")
        elif ev not in shown:
            out.append(f"{where}: {ev} was not in the results you were given")
    return out


def finding_problems(f: Finding, where: str, known: Mapping[str, str], shown: Collection[str]) -> list[str]:
    problems = _id_problems(f.evidence_ids, known, shown, where)
    if f.kind != "hypothesis" and not f.evidence_ids:
        problems.append(f"{where}: a {f.kind} needs at least one evidence id (or call it a hypothesis)")
    tools = {known[ev] for ev in f.evidence_ids if ev in known}
    live = tools - KNOWLEDGE_TOOLS  # a runbook is no signal of its own
    if f.kind != "hypothesis" and tools and not live:
        problems.append(f"{where}: a {f.kind} {KNOWLEDGE_ONLY}")
    elif live and not live & SIGNAL_TOOLS[f.signal]:
        problems.append(
            f"{where}: signal is {f.signal} but it cites only {', '.join(sorted(tools))} results; "
            f"cite a {'/'.join(sorted(SIGNAL_TOOLS[f.signal]))} result or fix the signal"
        )
    return problems


def check_report(
    report: InvestigatorReport, known: Mapping[str, str], shown: Collection[str]
) -> Checked[InvestigatorReport]:
    """``known``: evidence id -> tool for the incident; ``shown``: ids this agent saw."""
    problems: list[str] = []
    kept: list[Finding] = []
    for i, f in enumerate(report.findings):
        p = finding_problems(f, f"findings[{i}]", known, shown)
        problems += p
        if not p:
            kept.append(f)
    dropped = len(report.findings) - len(kept)
    return Checked(report.model_copy(update={"findings": kept}), problems, dropped)


class _Cited(Protocol):
    evidence_ids: list[str]


def _keep[C: _Cited](
    items: list[C],
    name: str,
    known: Mapping[str, str],
    shown: Collection[str],
    problems: list[str],
    *,
    live: bool = False,
) -> list[C]:
    """``live``: an item must cite at least one result that is not from the knowledge base."""
    out = []
    for i, item in enumerate(items):
        p = _id_problems(item.evidence_ids, known, shown, f"{name}[{i}]")
        tools = {known[ev] for ev in item.evidence_ids if ev in known}
        if live and tools and tools <= KNOWLEDGE_TOOLS:
            p.append(f"{name}[{i}]: {KNOWLEDGE_ONLY}")
        problems.extend(p)
        if not p:
            out.append(item)
    return out


def check_diagnosis(diagnosis: Diagnosis, known: Mapping[str, str], shown: Collection[str]) -> Checked[Diagnosis]:
    """``shown``: the evidence ids cited by the accepted findings."""
    problems: list[str] = []
    root_causes = _keep(diagnosis.root_causes, "root_causes", known, shown, problems, live=True)
    facts = _keep(diagnosis.facts, "facts", known, shown, problems, live=True)
    inferences = _keep(diagnosis.inferences, "inferences", known, shown, problems, live=True)
    dropped = (
        (len(diagnosis.root_causes) - len(root_causes))
        + (len(diagnosis.facts) - len(facts))
        + (len(diagnosis.inferences) - len(inferences))
    )
    value = diagnosis.model_copy(update={"root_causes": root_causes, "facts": facts, "inferences": inferences})
    return Checked(value, problems, dropped)


def check_plan(plan: RemediationPlan, known: Mapping[str, str], shown: Collection[str]) -> Checked[RemediationPlan]:
    """``shown``: the planner's own results plus the evidence the diagnosis cites."""
    problems: list[str] = []
    actions = _keep(plan.actions, "actions", known, shown, problems)
    return Checked(plan.model_copy(update={"actions": actions}), problems, len(plan.actions) - len(actions))
