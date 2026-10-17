"""The policy engine: may this plan run, and does a human have to approve it? (ADR-0007)

A pure function of structured fields: the actions and their arguments, the evidence they
cite (which tool, which arguments), the diagnosis's ranked root-cause services and their
confidence, the incident's service, and configuration. It never reads what the model wrote
in prose (summaries, reasons): a log line that talks the planner into "roll back payment"
still has to get past checks that prose cannot influence.

Every check reports ok or not with a reason. A failed blocking check denies the plan; so
does any error in here (fail closed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time
from typing import Any, Literal

from app.remediation.actions import CATALOGUE, Risk, risk_rank
from app.remediation.plans import RemediationPlan

Verdict = Literal["allow", "require_approval", "deny"]


@dataclass(frozen=True)
class EvidenceRef:
    tool: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class PolicyContext:
    incident_service: str
    # The diagnosis's candidates, most likely first: (service, confidence).
    root_causes: tuple[tuple[str, float], ...]
    # This incident's evidence rows: id -> what was queried.
    evidence: Mapping[str, EvidenceRef]
    namespace: str
    allowed_targets: frozenset[str]
    min_confidence: float
    auto_max_risk: Risk
    change_window: str  # "HH:MM-HH:MM", UTC; may wrap past midnight
    now: datetime


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True
    action: int | None = None  # index into plan.actions, for per-action checks


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    risk: Risk | None
    checks: list[Check] = field(default_factory=list)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def reasons(self) -> str:
        bad = [c for c in self.failed() if c.blocking] or self.failed()
        return "; ".join(c.detail for c in bad) or "all checks passed"

    def checks_json(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.checks]


def evaluate(plan: RemediationPlan, ctx: PolicyContext) -> Decision:
    try:
        return _evaluate(plan, ctx)
    except Exception as e:  # fail closed, whatever went wrong
        return Decision("deny", None, [Check("policy_error", False, f"the policy could not evaluate the plan: {e!r}")])


def _evaluate(plan: RemediationPlan, ctx: PolicyContext) -> Decision:
    if not plan.actions:
        return Decision("deny", None, [Check("actions", False, "the plan has no actions")])
    checks: list[Check] = []
    risks: list[Risk] = []
    for i, action in enumerate(plan.actions):
        info = CATALOGUE.get(action.action)
        if info is None:
            checks.append(Check("catalogue", False, f"{action.action} is not in the action catalogue", action=i))
            continue
        risks.append(info.risk)
        checks.append(Check("catalogue", info.risk != "R4", f"{action.action} is {info.risk}", action=i))
        checks.append(_scope(action.deployment, ctx, i))
        checks.append(_problem_host(action.deployment, ctx, i))
        checks.append(_evidence(action.action, action.deployment, action.evidence_ids, info.evidence_tools, ctx, i))
        checks.append(Check("rollback_plan", bool(info.inverse), f"undo: {info.inverse or 'none defined'}", action=i))
        if info.risk == "R3":
            checks.append(_alternative(plan, info.risk, i))
    checks.append(_window(ctx))

    risk: Risk | None = None
    for r in risks:
        if risk is None or risk_rank(r) > risk_rank(risk):
            risk = r
    if any(not c.ok and c.blocking for c in checks) or risk is None:
        return Decision("deny", risk, checks)
    verdict: Verdict = "allow" if risk_rank(risk) <= risk_rank(ctx.auto_max_risk) else "require_approval"
    return Decision(verdict, risk, checks)


def _scope(deployment: str, ctx: PolicyContext, i: int) -> Check:
    if deployment not in ctx.allowed_targets:
        return Check("scope", False, f"{deployment} is not on the list of Deployments remediation may touch", action=i)
    return Check("scope", True, f"{ctx.namespace}/{deployment} may be remediated", action=i)


def _problem_host(deployment: str, ctx: PolicyContext, i: int) -> Check:
    """B10: act where the problem is, not where it shows (the incident's own service may only be a symptom)."""
    if not ctx.root_causes:
        return Check("problem_host", False, "the diagnosis has no root cause", action=i)
    top, confidence = ctx.root_causes[0]
    if deployment != top:
        where = " (the alerting service, which shows the symptoms)" if deployment == ctx.incident_service else ""
        return Check("problem_host", False, f"{deployment}{where} is not the top root cause ({top})", action=i)
    if confidence < ctx.min_confidence:
        return Check(
            "problem_host",
            False,
            f"the top root cause {top} has confidence {confidence:.2f}, below {ctx.min_confidence:.2f}",
            action=i,
        )
    return Check("problem_host", True, f"{deployment} is the top root cause ({confidence:.0%})", action=i)


def _evidence(kind: str, deployment: str, ids: list[str], tools: tuple[str, ...], ctx: PolicyContext, i: int) -> Check:
    unknown = [ev for ev in ids if ev not in ctx.evidence]
    if unknown:
        return Check("evidence", False, f"cited evidence not found for this incident: {', '.join(unknown)}", action=i)
    if tools:
        about = [
            ev for ev in ids if ctx.evidence[ev].tool in tools and ctx.evidence[ev].args.get("deployment") == deployment
        ]
        if not about:
            return Check(
                "evidence",
                False,
                f"{kind} needs a cited {' or '.join(tools)} result for {deployment}, showing what would be undone",
                action=i,
            )
    return Check("evidence", True, f"{len(ids)} cited result(s) of this incident", action=i)


def _alternative(plan: RemediationPlan, risk: Risk, i: int) -> Check:
    lower = [
        a.action
        for a in plan.alternatives
        if a.action in CATALOGUE and risk_rank(CATALOGUE[a.action].risk) < risk_rank(risk)
    ]
    if lower:
        return Check("alternative", True, f"lower-risk options considered: {', '.join(sorted(set(lower)))}", False, i)
    return Check("alternative", False, f"a {risk} action without any lower-risk option considered", False, i)


def _window(ctx: PolicyContext) -> Check:
    start_s, _, end_s = ctx.change_window.partition("-")
    start, end = _clock(start_s), _clock(end_s)
    utc = ctx.now.astimezone(UTC)
    now = utc.time()
    inside = start <= now < end if start <= end else (now >= start or now < end)
    where = "inside" if inside else "outside"
    return Check("change_window", inside, f"{utc:%H:%M} UTC is {where} the change window {ctx.change_window} UTC")


def _clock(value: str) -> time:
    hours, _, minutes = value.strip().partition(":")
    if int(hours) == 24 and int(minutes or 0) == 0:
        return time.max
    return time(int(hours), int(minutes or 0))
