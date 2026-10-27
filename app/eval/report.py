"""The evaluation report, as Markdown: the gate, each variant's metrics, each scenario's answers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from app.eval import scenarios
from app.eval.gate import GateResult
from app.eval.metrics import SUMMARY_FIELDS, Stat, VariantMetrics, summarise
from app.eval.score import ScenarioScore

LABELS = {
    "top1": "Top-1",
    "top3": "Top-3",
    "incident_accuracy": "per incident",
    "action_accuracy": "actions right",
    "safety_violations": "safety violations",
    "citation_first_pass": "citations ok 1st try",
    "claims_dropped": "claims dropped",
    "diagnosis_p50_s": "diagnosis p50",
    "diagnosis_p95_s": "diagnosis p95",
    "tokens_per_incident": "tokens / incident",
    "cost_per_incident": "$ / incident",
    "escalation_rate": "escalated",
}
PERCENT = {
    "top1",
    "top3",
    "incident_accuracy",
    "action_accuracy",
    "citation_first_pass",
    "claims_dropped",
    "escalation_rate",
}


def _fmt(name: str, v: float) -> str:
    if name in PERCENT:
        return f"{v:.0%}"
    if name.endswith("_s"):
        return f"{v:.0f}s"
    if name == "cost_per_incident":
        return f"${v:.3f}"
    if name == "tokens_per_incident":
        return f"{v / 1000:.0f}k"
    return f"{v:.0f}" if v == int(v) else f"{v:.1f}"


def _stat(name: str, s: Stat | None) -> str:
    if s is None:
        return "-"
    text = _fmt(name, s.mean)
    if s.sd is not None:
        text += f" ± {_fmt(name, s.sd)}"
    return text


def render(
    scores: Sequence[ScenarioScore], per_run: dict[str, list[VariantMetrics]], gates: Sequence[GateResult]
) -> str:
    variants = list(per_run)
    lines = ["### Gate", ""]
    for g in gates:
        verdict = "pass" if g.passed else "FAIL: " + "; ".join(g.reasons)
        top = f"Top-1 {g.top1:.0%}" if g.top1 is not None else "no scenario scored"
        vs = f" vs baseline {g.baseline_top1:.0%} on {g.common} scenarios" if g.baseline_top1 is not None else ""
        lines.append(f"- **{g.variant}**: {top}{vs}: {verdict}")

    runs = {v: len(m) for v, m in per_run.items()}
    lines += [
        "",
        f"### Metrics (mean ± sd over runs: {', '.join(f'{v} {n}' for v, n in runs.items())})",
        "",
        "| metric | " + " | ".join(variants) + " |",
        "|---|" + "---|" * len(variants),
    ]
    summaries = {v: summarise(m) for v, m in per_run.items()}
    for name in SUMMARY_FIELDS:
        lines.append(f"| {LABELS[name]} | " + " | ".join(_stat(name, summaries[v][name]) for v in variants) + " |")
    scored = {v: sum(m.scenarios for m in ms) for v, ms in per_run.items()}
    lines.append("| scenarios scored (all runs) | " + " | ".join(str(scored[v]) for v in variants) + " |")

    # Each scenario: the top answer of every run, per variant.
    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s in sorted(scores, key=lambda s: s.run):
        mark = "-" if not s.alerted else ("✓" if s.top1 else "✗")
        cells[(s.scenario, s.variant)].append(f"{mark} {s.answer or ('no alert' if not s.alerted else 'none')}")
    lines += [
        "",
        "### Scenarios (top candidate of each run; ✓ = the labelled root cause)",
        "",
        "| scenario | difficulty | root cause | " + " | ".join(variants) + " |",
        "|---|---|---|" + "---|" * len(variants),
    ]
    for sc in scenarios.catalogue():
        if not any((sc.id, v) in cells for v in variants):
            continue
        row = [sc.id, sc.difficulty, sc.expect.root_cause]
        row += ["<br>".join(cells.get((sc.id, v), [])) or "-" for v in variants]
        lines.append("| " + " | ".join(row) + " |")

    problems = [s for s in scores if s.violations or (s.alerted and not s.action_correct)]
    if problems:
        lines += [
            "",
            "### Actions",
            "",
            "| scenario | variant | run | put to a human | right | safety |",
            "|---|---|---|---|---|---|",
        ]
        for s in problems:
            lines.append(
                f"| {s.scenario} | {s.variant} | {s.run} | {', '.join(s.approvable) or 'nothing'} "
                f"| {'yes' if s.action_correct else 'no'} | {', '.join(s.violations) or 'ok'} |"
            )
    return "\n".join(lines) + "\n"
