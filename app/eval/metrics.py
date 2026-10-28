"""Scores of many scenarios -> one variant's metrics, and their spread over repeated runs.

Rates are over the scenarios that opened at least one incident: a scenario the lab failed
to turn into an alert says something about the lab, not about the agents, and is listed
separately (``not_alerted``). Repeats are summarised as mean and sample standard deviation;
with three runs the deviation is rough, and is reported as such.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.eval.score import ScenarioScore


class VariantMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant: str
    scenarios: int
    not_alerted: list[str] = Field(default_factory=list)
    model_unavailable: list[str] = Field(default_factory=list)  # not scored: no model to ask
    top1: float = 0.0
    top3: float = 0.0
    incident_accuracy: float = 0.0
    action_accuracy: float = 0.0
    safety_violations: int = 0
    citation_first_pass: float | None = None
    claims_dropped: float | None = None
    diagnosis_p50_s: float | None = None
    diagnosis_p95_s: float | None = None
    tokens_per_incident: float = 0.0
    cost_per_incident: float = 0.0
    escalation_rate: float = 0.0
    top1_by_difficulty: dict[str, float] = Field(default_factory=dict)


# The numbers a summary over repeated runs reports, in this order.
SUMMARY_FIELDS = (
    "top1",
    "top3",
    "incident_accuracy",
    "action_accuracy",
    "safety_violations",
    "citation_first_pass",
    "claims_dropped",
    "diagnosis_p50_s",
    "diagnosis_p95_s",
    "tokens_per_incident",
    "cost_per_incident",
    "escalation_rate",
)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest rank: the smallest value with at least ``q`` of the values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def variant_metrics(variant: str, scores: Iterable[ScenarioScore]) -> VariantMetrics:
    all_scores = [s for s in scores if s.variant == variant]
    scored = [s for s in all_scores if s.scored]
    m = VariantMetrics(
        variant=variant,
        scenarios=len(scored),
        not_alerted=sorted(s.scenario for s in all_scores if not s.alerted),
        model_unavailable=sorted(s.scenario for s in all_scores if s.alerted and s.model_unavailable),
    )
    m.safety_violations = sum(len(s.violations) for s in all_scores)  # whatever paged
    if not scored:
        return m
    n = len(scored)
    incidents = sum(s.incidents for s in scored)
    m.top1 = sum(s.top1 for s in scored) / n
    m.top3 = sum(s.top3 for s in scored) / n
    m.incident_accuracy = _ratio(sum(s.incidents_correct for s in scored), incidents) or 0.0
    m.action_accuracy = sum(s.action_correct for s in scored) / n
    m.citation_first_pass = _ratio(sum(s.first_pass for s in scored), sum(s.answers for s in scored))
    m.claims_dropped = _ratio(sum(s.dropped for s in scored), sum(s.kept + s.dropped for s in scored))
    times = [s.diagnosis_s for s in scored if s.diagnosis_s is not None]
    m.diagnosis_p50_s = percentile(times, 0.5)
    m.diagnosis_p95_s = percentile(times, 0.95)
    m.tokens_per_incident = sum(s.tokens for s in scored) / incidents
    m.cost_per_incident = sum(s.cost_usd for s in scored) / incidents
    m.escalation_rate = sum(s.escalated for s in scored) / incidents
    for level in ("easy", "medium", "hard"):
        mine = [s.top1 for s in scored if s.difficulty == level]
        if mine:
            m.top1_by_difficulty[level] = sum(mine) / len(mine)
    return m


class Stat(BaseModel):
    mean: float
    sd: float | None  # None for a single run
    values: list[float]


def summarise(runs: Sequence[VariantMetrics]) -> dict[str, Stat | None]:
    """Mean and standard deviation of each metric over repeated runs of one variant."""
    out: dict[str, Stat | None] = {}
    for name in SUMMARY_FIELDS:
        values = [float(v) for r in runs if (v := getattr(r, name)) is not None]
        if not values:
            out[name] = None
            continue
        sd = statistics.stdev(values) if len(values) > 1 else None
        out[name] = Stat(mean=statistics.fmean(values), sd=sd, values=values)
    return out
