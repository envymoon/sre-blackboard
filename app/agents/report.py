"""The diagnosis as Markdown, for people. The structured Diagnosis stays the source of truth;
this is a template over it, keeping facts, inferences and open hypotheses apart, and linking
every evidence id to its stored snapshot."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agents.schemas import Diagnosis, Finding
from app.agents.store import RunRecord


@dataclass(frozen=True)
class ReportInput:
    incident: Mapping[str, Any]  # incidents row (title, service, priority name, state, created_at, ...)
    diagnosis: Diagnosis | None
    findings: Sequence[tuple[str, Finding]]
    evidence: Mapping[str, Mapping[str, Any]]  # id -> tool, args, result_hash, fetched_at, source
    runs: Mapping[str, RunRecord]
    usage: Mapping[str, Any] = field(default_factory=dict)  # calls, tokens, cost_usd


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(data: ReportInput, evidence_url: Callable[[str], str] = lambda ev: f"/api/v1/evidence/{ev}") -> str:
    inc = data.incident

    def cite(ids: Sequence[str]) -> str:
        return ", ".join(f"[{ev}]({evidence_url(ev)})" for ev in ids) or "none"

    out = [
        f"# {inc['title']} on {inc['service']}",
        "",
        f"Priority {inc['priority']} · state {inc['state']} · environment {inc['environment']} · "
        f"opened {inc['created_at']:%Y-%m-%d %H:%M:%S %Z} · incident `{inc['id']}`",
        "",
    ]
    dx = data.diagnosis
    if dx is None:
        out += ["No diagnosis yet.", ""]
    else:
        out += ["## Summary", "", dx.summary, "", "## Root cause candidates", ""]
        if dx.root_causes:
            out += ["| # | service | cause | confidence | evidence |", "|---|---|---|---|---|"]
            out += [
                f"| {i} | {_cell(r.service)} | {_cell(r.cause)} | {r.confidence:.0%} | {cite(r.evidence_ids)} |"
                for i, r in enumerate(dx.root_causes, 1)
            ]
        else:
            out.append("None is supported by the evidence; a person needs to take over.")
        out += ["", "## Facts", "", "Shown directly by the evidence.", ""]
        out += [f"- {c.statement} ({cite(c.evidence_ids)})" for c in dx.facts] or ["- none"]
        out += ["", "## High-confidence inferences", ""]
        out += [f"- {c.statement} ({cite(c.evidence_ids)})" for c in dx.inferences] or ["- none"]
        out += ["", "## Hypotheses to verify", ""]
        out += [f"- {h.statement} To check: {h.how_to_verify}" for h in dx.hypotheses] or ["- none"]
        out.append("")

    if data.findings:
        out += ["## Findings by investigator", ""]
        for author in sorted({a for a, _ in data.findings}):
            out += [f"### {author}", ""]
            for f in (f for a, f in data.findings if a == author):
                out.append(f"- **{f.kind}** ({f.signal}, {f.confidence:.0%}): {f.statement} ({cite(f.evidence_ids)})")
            out.append("")

    if data.evidence:
        out += [
            "## Evidence",
            "",
            "| id | tool | arguments | result sha256 | fetched | source |",
            "|---|---|---|---|---|---|",
        ]
        for ev, e in sorted(data.evidence.items()):
            args = json.dumps(e["args"], sort_keys=True, ensure_ascii=False)
            out.append(
                f"| [{ev}]({evidence_url(ev)}) | {e['tool']} | `{_cell(args[:160])}` | `{e['result_hash'][:12]}` "
                f"| {e['fetched_at']:%H:%M:%S} | {e['source']} |"
            )
        out.append("")

    if data.runs:
        out += [
            "## Agent runs",
            "",
            "| agent | status | model | prompt | steps | tool calls | dropped |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, r in data.runs.items():
            out.append(
                f"| {name} | {r.status} | {r.model} | `{r.prompt_version}` | {r.steps} "
                f"| {r.result.get('tool_calls', '-')} | {r.result.get('dropped', '-')} |"
            )
        out.append("")
    if data.usage:
        cost = data.usage.get("cost_usd")
        out.append(
            f"Model calls: {data.usage.get('calls', 0)}, tokens: {data.usage.get('tokens', 0):,}"
            + (f", equivalent API cost: ${float(cost):.4f}" if cost is not None else "")
        )
    return "\n".join(out).rstrip() + "\n"
