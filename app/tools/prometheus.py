"""prom_query and prom_range: PromQL against the lab's Prometheus.

Useful series in the lab (recorded by deploy/kind/rules/slo.yml, one per service_name):
service:sli_error_ratio:rate5m, service:sli_requests:rate5m, service:sli_latency_p95_ms:rate5m.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import Field

from app.tools.base import Tool, ToolArgs, ToolError
from app.tools.clients import Clients, get_json
from app.tools.compress import iso, num

TOP = 10
SHAPE_POINTS = 12


class PromQueryArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=2000, description="PromQL expression")
    at: datetime | None = Field(None, description="Evaluation time (RFC 3339); default now")


class PromRangeArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=2000, description="PromQL expression")
    minutes: int = Field(30, ge=1, le=360, description="Window length, ending at `end`")
    step_s: int | None = Field(None, ge=5, le=3600, description="Resolution; default gives about 120 points")
    end: datetime | None = Field(None, description="Window end (RFC 3339); default now")


async def _query(c: Clients, args: PromQueryArgs, now: datetime) -> Any:
    at = args.at or now
    body = await get_json(c.prometheus, "/api/v1/query", params={"query": args.query, "time": at.timestamp()})
    return _data(body)


async def _range(c: Clients, args: PromRangeArgs, now: datetime) -> Any:
    end = args.end or now
    start = end - timedelta(minutes=args.minutes)
    step = args.step_s or max(15, args.minutes * 60 // 120)
    params = {"query": args.query, "start": start.timestamp(), "end": end.timestamp(), "step": step}
    return _data(await get_json(c.prometheus, "/api/v1/query_range", params=params)) | {"step": step}


def _data(body: Any) -> dict[str, Any]:
    if body.get("status") != "success":
        raise ToolError("bad_query", str(body.get("error", body))[:500])
    data: dict[str, Any] = body["data"]
    return data


def _labels(metric: dict[str, str]) -> dict[str, str]:
    return dict(metric)


def summarize_query(raw: dict[str, Any], args: PromQueryArgs) -> dict[str, Any]:
    kind = raw["resultType"]
    result = raw["result"]
    if kind in ("scalar", "string"):
        return {"result_type": kind, "value": num(result[1]) if kind == "scalar" else result[1]}
    series: list[dict[str, Any]] = [{"labels": _labels(s["metric"]), "value": num(s["value"][1])} for s in result]
    series.sort(key=lambda s: (s["value"] is None, -(s["value"] or 0)))
    out: dict[str, Any] = {"result_type": kind, "series_count": len(series), "top": series[:TOP]}
    if len(series) > TOP:
        out["note"] = f"showing the {TOP} highest of {len(series)} series"
    if not series:
        out["note"] = "no data: the selector matched nothing at this time"
    return out


def summarize_range(raw: dict[str, Any], args: PromRangeArgs) -> dict[str, Any]:
    stats = [st for st in (_series_stats(s) for s in raw["result"]) if st is not None]
    stats.sort(key=lambda s: -(s["max"] or 0))
    out: dict[str, Any] = {
        "series_count": len(stats),
        "step_s": raw.get("step"),
        "minutes": args.minutes,
        "series": stats[:TOP],
    }
    if len(stats) > TOP:
        out["note"] = f"showing the {TOP} series with the highest peak of {len(stats)}"
    if not stats:
        out["note"] = "no data in this window"
    return out


def _series_stats(series: dict[str, Any]) -> dict[str, Any] | None:
    points = [(float(t), num(v)) for t, v in series["values"]]
    known = [(t, v) for t, v in points if v is not None]
    if not known:
        return None
    values = [v for _, v in known]
    peak_t = max(known, key=lambda p: p[1])[0]
    return {
        "labels": _labels(series["metric"]),
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "mean": num(sum(values) / len(values)),
        "peak_at": iso(peak_t),
        "from": iso(points[0][0]),
        "to": iso(points[-1][0]),
        # The series' shape in SHAPE_POINTS buckets (bucket means), oldest first.
        "shape": _shape([v for _, v in points]),
    }


def _shape(values: list[float | None]) -> list[float | None]:
    if len(values) <= SHAPE_POINTS:
        return values
    out: list[float | None] = []
    for i in range(SHAPE_POINTS):
        lo = i * len(values) // SHAPE_POINTS
        hi = (i + 1) * len(values) // SHAPE_POINTS
        bucket = [v for v in values[lo:hi] if v is not None]
        out.append(num(sum(bucket) / len(bucket)) if bucket else None)
    return out


PROM_QUERY = Tool(
    name="prom_query",
    description=(
        "Evaluate a PromQL expression at one instant. Returns the highest series with their labels. "
        "Per-service SLIs: service:sli_error_ratio:rate5m, service:sli_requests:rate5m, "
        "service:sli_latency_p95_ms:rate5m (label service_name)."
    ),
    args=PromQueryArgs,
    backend="prometheus",
    fetch=_query,
    summarize=summarize_query,
)

PROM_RANGE = Tool(
    name="prom_range",
    description=(
        "Evaluate a PromQL expression over a time window. For each series: first, last, min, max, mean, "
        "when it peaked, and its shape in 12 buckets. Use it to see when something changed."
    ),
    args=PromRangeArgs,
    backend="prometheus",
    fetch=_range,
    summarize=summarize_range,
    timeout_s=15.0,
)
