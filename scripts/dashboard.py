"""The Grafana dashboard for sre-blackboard itself (M8, ADR-0011), as code.

``python -m scripts.dashboard`` writes deploy/observability/grafana/dashboards/sre-blackboard.json,
which Grafana loads at start (deploy/observability/grafana/provisioning). Edit the panels here,
not in the JSON: a test fails when the two differ, and another when a panel or an alert uses a
metric app/obs does not define, or a metric is on no panel.

Gauges read from Postgres (sre_queue_*, sre_incidents_*) are reported by every API container,
so they are taken with max() before any sum. Histogram panels ask for exemplars: a point on
the latency graph opens the trace it came from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = (
    Path(__file__).resolve().parents[1] / "deploy" / "observability" / "grafana" / "dashboards" / "sre-blackboard.json"
)
DS = {"type": "prometheus", "uid": "sre-prometheus"}
RATE = "$__rate_interval"


def q(p: float, metric: str, by: str) -> str:
    return f"histogram_quantile({p}, sum by (le, {by}) (rate({metric}_bucket[{RATE}])))"


class Dashboard:
    def __init__(self) -> None:
        self.panels: list[dict[str, Any]] = []
        self.x = 0
        self.y = 0

    def _id(self) -> int:
        return len(self.panels) + 1

    def _place(self, w: int, h: int) -> dict[str, int]:
        if self.x + w > 24:
            self.y += 8
            self.x = 0
        pos = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.x += w
        return pos

    def row(self, title: str) -> None:
        if self.x:
            self.y += 8
            self.x = 0
        self.panels.append(
            {
                "type": "row",
                "title": title,
                "id": self._id(),
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y},
                "panels": [],
            }
        )
        self.y += 1

    def newline(self) -> None:
        self.x = 24

    def series(
        self,
        title: str,
        targets: list[tuple[str, str]],
        unit: str = "short",
        *,
        w: int = 8,
        desc: str = "",
        exemplars: bool = False,
        stack: bool = False,
    ) -> None:
        custom: dict[str, Any] = {"fillOpacity": 10, "lineWidth": 1, "showPoints": "never"}
        if stack:
            custom["stacking"] = {"mode": "normal"}
        self.panels.append(
            {
                "type": "timeseries",
                "title": title,
                "id": self._id(),
                "datasource": DS,
                "description": desc,
                "gridPos": self._place(w, 8),
                "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": []},
                "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
                "targets": [
                    {
                        "refId": chr(65 + i),
                        "datasource": DS,
                        "expr": expr,
                        "legendFormat": legend,
                        **({"exemplar": True} if exemplars else {}),
                    }
                    for i, (expr, legend) in enumerate(targets)
                ],
            }
        )

    def stat(
        self,
        title: str,
        expr: str,
        unit: str = "short",
        *,
        thresholds: list[tuple[float, str]] | None = None,
        desc: str = "",
    ) -> None:
        steps: list[dict[str, Any]] = [{"color": "green", "value": None}]
        steps += [{"color": color, "value": v} for v, color in thresholds or []]
        self.panels.append(
            {
                "type": "stat",
                "title": title,
                "id": self._id(),
                "datasource": DS,
                "description": desc,
                "gridPos": self._place(4, 4),
                "fieldConfig": {
                    "defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}},
                    "overrides": [],
                },
                "options": {
                    "reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "background",
                    "graphMode": "none",
                },
                "targets": [{"refId": "A", "datasource": DS, "expr": expr, "instant": True}],
            }
        )


def build() -> dict[str, Any]:
    d = Dashboard()

    d.row("Incidents")
    d.stat(
        "Open incidents", "sum(max by (state, priority) (sre_incidents_open))", desc="From Postgres, at scrape time."
    )
    d.stat(
        "Oldest P0 without a diagnosis",
        'max(sre_incidents_undiagnosed_oldest_seconds{priority="P0"})',
        "s",
        thresholds=[(60, "orange"), (120, "red")],
        desc="P0DiagnosisLate pages above 120 s.",
    )
    d.stat(
        "P0 diagnosed within 2 min (1h)",
        "sre:p0_diagnosed_within_2m:ratio_1h",
        "percentunit",
        desc="The objective is 90%. Escalations count as misses.",
    )
    d.stat(
        "Oldest pending approval",
        "max(sre_approvals_pending_oldest_seconds)",
        "s",
        thresholds=[(900, "orange"), (1800, "red")],
    )
    d.stat(
        "Queue state readable",
        "max(sre_state_up)",
        desc="1: the API read the queue and the incidents from Postgres when it was last scraped.",
    )
    d.stat("Targets up", 'sum(up{job=~"sre-.*"})')
    d.newline()
    d.series(
        "Time to first diagnosis (p50, p95)",
        [
            (q(0.5, "sre_incident_diagnosis_seconds", "priority"), "p50 {{priority}}"),
            (q(0.95, "sre_incident_diagnosis_seconds", "priority"), "p95 {{priority}}"),
        ],
        "s",
        desc="From opening the incident to DIAGNOSED (or ESCALATED instead).",
        exemplars=True,
    )
    d.series("Time in each state (p95)", [(q(0.95, "sre_incident_state_duration_seconds", "state"), "{{state}}")], "s")
    d.series("Time to close (p50)", [(q(0.5, "sre_incident_close_seconds", "state"), "{{state}}")], "s")
    d.series(
        "Incidents opened per hour",
        [(f"sum by (priority) (rate(sre_incidents_opened_total[{RATE}])) * 3600", "{{priority}}")],
        stack=True,
    )
    d.series(
        "Alerts received",
        [(f"sum by (result) (rate(sre_alerts_received_total[{RATE}]))", "{{result}}")],
        "reqps",
        desc="merged: a repeat of an open incident's alert.",
    )
    d.series(
        "State changes",
        [(f"sum by (to_state) (rate(sre_incident_transitions_total[{RATE}]))", "to {{to_state}}")],
        "ops",
    )

    d.row("Queue and workers")
    d.series(
        "Jobs waiting, by priority",
        [('sum by (priority) (max by (kind, status, priority) (sre_queue_jobs{status="queued"}))', "{{priority}}")],
        stack=True,
    )
    d.series(
        "Oldest ready job, by priority",
        [("max by (priority) (sre_queue_ready_oldest_seconds)", "{{priority}}")],
        "s",
        desc="Ready to run, not claimed. QueueBacklog fires above 60 s for 2 minutes.",
    )
    d.series("Queue wait (p95)", [(q(0.95, "sre_queue_wait_seconds", "priority"), "{{priority}}")], "s")
    d.series(
        "Job attempts, by outcome",
        [(f"sum by (kind, outcome) (rate(sre_jobs_total[{RATE}]))", "{{kind}} {{outcome}}")],
        "ops",
    )
    d.series(
        "Job duration (p95)",
        [(q(0.95, "sre_job_duration_seconds", "kind"), "{{kind}}")],
        "s",
        exemplars=True,
        desc="An exemplar opens the job's trace.",
    )
    d.series(
        "Worker slots in use, dead jobs",
        [
            ("sum(sre_worker_jobs_running) / sum(sre_worker_slots)", "slots in use"),
            ('sum(max by (kind, priority) (sre_queue_jobs{status="dead"}))', "dead jobs"),
        ],
    )

    d.row("Model and agents")
    d.series(
        "Tokens per minute, by agent",
        [(f"sum by (agent) (rate(sre_llm_tokens_total[{RATE}])) * 60", "{{agent}}")],
        stack=True,
    )
    d.series(
        "Tokens per hour, by kind",
        [(f"sum by (type) (rate(sre_llm_tokens_total[{RATE}])) * 3600", "{{type}}")],
        stack=True,
        desc="cache_read is most of it: every step sends the whole transcript again.",
    )
    d.series(
        "Cost per hour (API prices)",
        [(f"sum by (model) (rate(sre_llm_cost_usd_total[{RATE}])) * 3600", "{{model}}")],
        "currencyUSD",
        desc="As the backend reports it (the CLI does, the API backend does not).",
    )
    d.series(
        "Model call errors",
        [
            (
                f'sum by (outcome) (rate(sre_llm_requests_total{{outcome!="ok"}}[{RATE}])) '
                f"/ ignoring (outcome) group_left sum(rate(sre_llm_requests_total[{RATE}]))",
                "{{outcome}}",
            )
        ],
        "percentunit",
        stack=True,
    )
    d.series(
        "Model call latency (p50, p95)",
        [
            (q(0.5, "sre_llm_request_duration_seconds", "model"), "p50 {{model}}"),
            (q(0.95, "sre_llm_request_duration_seconds", "model"), "p95 {{model}}"),
        ],
        "s",
        exemplars=True,
    )
    d.series(
        "Agent runs, by outcome",
        [(f"sum by (agent, outcome) (rate(sre_agent_runs_total[{RATE}]))", "{{agent}} {{outcome}}")],
        "ops",
    )
    d.series("Steps per agent run (p95)", [(q(0.95, "sre_agent_steps", "agent"), "{{agent}}")])
    d.series(
        "Findings dropped by the citation checks",
        [(f"sum by (agent) (rate(sre_agent_findings_dropped_total[{RATE}])) * 3600", "{{agent}}")],
        desc="Per hour. A rise after a prompt change: the model cites what it did not query.",
    )

    d.row("Tools")
    d.series(
        "Tool calls, by tool", [(f"sum by (tool) (rate(sre_tool_calls_total[{RATE}]))", "{{tool}}")], "ops", stack=True
    )
    d.series(
        "Tool errors, by code",
        [
            (
                f'sum by (tool, outcome) (rate(sre_tool_calls_total{{outcome!="ok"}}[{RATE}])) '
                f"/ ignoring (outcome) group_left sum by (tool) (rate(sre_tool_calls_total[{RATE}]))",
                "{{tool}} {{outcome}}",
            )
        ],
        "percentunit",
        desc="invalid_args, bad_query, too_large: the agent asked badly. timeout, unavailable, backend_error: the source is failing.",
    )
    d.series("Tool latency (p95)", [(q(0.95, "sre_tool_duration_seconds", "tool"), "{{tool}}")], "s")

    d.row("Remediation")
    d.series(
        "Policy verdicts per hour",
        [(f"sum by (stage, verdict) (rate(sre_policy_decisions_total[{RATE}])) * 3600", "{{stage}} {{verdict}}")],
        desc="At submission, and again in the executor before anything runs.",
    )
    d.series(
        "Approval wait (p50, p95)",
        [
            (q(0.5, "sre_approval_wait_seconds", "decision"), "p50 {{decision}}"),
            (q(0.95, "sre_approval_wait_seconds", "decision"), "p95 {{decision}}"),
        ],
        "s",
    )
    d.series(
        "Executions and verifications per hour",
        [
            ("sum by (action, outcome) (increase(sre_executions_total[1h]))", "{{action}} {{outcome}}"),
            ("sum by (outcome) (increase(sre_verifications_total[1h]))", "verification {{outcome}}"),
        ],
    )

    d.row("API")
    d.series(
        "Requests, by route and status",
        [(f"sum by (route, status) (rate(sre_http_requests_total[{RATE}]))", "{{route}} {{status}}")],
        "reqps",
        w=12,
    )
    d.series(
        "Request latency (p95), by route",
        [(q(0.95, "sre_http_request_duration_seconds", "route"), "{{route}}")],
        "s",
        w=12,
    )

    return {
        "uid": "sre-blackboard",
        "title": "sre-blackboard",
        "description": "The system's own health (M8, ADR-0011). Generated by scripts/dashboard.py; do not edit by hand.",
        "tags": ["sre-blackboard"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": []},
        "annotations": {"list": []},
        "links": [],
        "panels": d.panels,
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main() -> None:
    OUT.write_text(render())
    print(f"wrote {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT}")


if __name__ == "__main__":
    main()
