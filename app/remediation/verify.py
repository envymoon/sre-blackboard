"""Did the repair work? The incident's SLI, read straight from Prometheus (ADR-0007).

Which query and which threshold come from the alert's ``slo`` label, never from the model.
The windows are 1 minute, shorter than the alerts' (5 minutes and a ``for``), so recovery
shows within a minute or two instead of waiting for the alert to drain. An SLO this table
does not know cannot be verified, and that is an escalation, not a pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

from app.tools.clients import get_json


@dataclass(frozen=True)
class SliCheck:
    name: str
    query: str  # with {service}
    healthy_below: float
    unit: str

    def promql(self, service: str) -> str:
        return self.query.replace("{service}", service)


SERVER = 'span_kind="SPAN_KIND_SERVER", service_name="{service}"'
CHECKS: dict[str, SliCheck] = {
    # The alert fires above 14.4% (fast burn); recovered means below the slow-burn line, 6%.
    "availability": SliCheck(
        "error ratio over 1m",
        f'sum(rate(traces_span_metrics_calls_total{{{SERVER}, status_code="STATUS_CODE_ERROR"}}[1m]))'
        f" / sum(rate(traces_span_metrics_calls_total{{{SERVER}}}[1m]))",
        0.06,
        "ratio",
    ),
    "latency": SliCheck(
        "p95 over 1m",
        f"histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{{{SERVER}}}[1m])))",
        1000.0,
        "ms",
    ),
}


def check_for(labels: dict[str, Any]) -> SliCheck | None:
    return CHECKS.get(str(labels.get("slo", "")))


async def read(prometheus: httpx.AsyncClient, check: SliCheck, service: str) -> float | None:
    """The SLI now; None when there is no data (no traffic, or no series yet)."""
    body = await get_json(prometheus, "/api/v1/query", params={"query": check.promql(service)})
    result = body.get("data", {}).get("result", [])
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if math.isnan(value) else value
