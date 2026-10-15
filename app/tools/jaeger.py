"""traces_search and trace_get: the Jaeger query API (served under /jaeger/ui in the demo)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from pydantic import Field

from app.tools.base import Tool, ToolArgs, ToolError
from app.tools.clients import Clients, get_json
from app.tools.compress import clip, num, percentile

TOP = 10
_MESSAGE_TAGS = ("otel.status_description", "grpc.error_message", "error.message", "exception.message")
_STATUS_TAGS = ("rpc.grpc.status_code", "http.response.status_code", "http.status_code", "error.type")


class TracesSearchArgs(ToolArgs):
    service: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$", description="Service the traces must touch")
    operation: str | None = Field(None, max_length=200, description="Span name, e.g. oteldemo.CartService/GetCart")
    errors_only: bool = Field(False, description="Only traces with an error span in this service")
    min_duration_ms: int | None = Field(None, ge=1, le=600_000)
    minutes: int = Field(15, ge=1, le=360)
    limit: int = Field(20, ge=1, le=50)
    end: datetime | None = Field(None, description="Window end (RFC 3339); default now")


class TraceGetArgs(ToolArgs):
    trace_id: str = Field(pattern=r"^[0-9a-fA-F]{16,32}$")


async def _search(c: Clients, args: TracesSearchArgs, now: datetime) -> Any:
    end = args.end or now
    start = end - timedelta(minutes=args.minutes)
    params: dict[str, Any] = {
        "service": args.service,
        "start": int(start.timestamp() * 1e6),
        "end": int(end.timestamp() * 1e6),
        "limit": args.limit,
    }
    if args.operation:
        params["operation"] = args.operation
    if args.errors_only:
        params["tags"] = json.dumps({"error": "true"})
    if args.min_duration_ms:
        params["minDuration"] = f"{args.min_duration_ms}ms"
    body = await get_json(c.jaeger, "/api/traces", params=params)
    return body.get("data") or []


async def _get(c: Clients, args: TraceGetArgs, now: datetime) -> Any:
    body = await get_json(c.jaeger, f"/api/traces/{args.trace_id.lower()}")
    data = body.get("data") or []
    if not data:
        raise ToolError("not_found", f"trace {args.trace_id} not found (it may have aged out)")
    return data[0]


# --- reading Jaeger's JSON -------------------------------------------------------------------


def _tags(span: dict[str, Any]) -> dict[str, Any]:
    return {t["key"]: t["value"] for t in span.get("tags", [])}


def _service(trace: dict[str, Any], span: dict[str, Any]) -> str:
    return str(trace["processes"].get(span["processID"], {}).get("serviceName", "?"))


def _is_error(tags: dict[str, Any]) -> bool:
    return tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR"


def _message(span: dict[str, Any], tags: dict[str, Any]) -> str:
    for key in _MESSAGE_TAGS:
        if tags.get(key):
            return str(tags[key])
    for log in span.get("logs", []):
        for f in log.get("fields", []):
            if f["key"] == "exception.message":
                return str(f["value"])
    return ""


def _status(tags: dict[str, Any]) -> str | None:
    found = [f"{k}={tags[k]}" for k in _STATUS_TAGS if k in tags]
    return " ".join(found) or None


def _duration_ms(trace: dict[str, Any]) -> float:
    spans = trace["spans"]
    start = min(s["startTime"] for s in spans)
    end = max(s["startTime"] + s["duration"] for s in spans)
    return (end - start) / 1000


def _root(trace: dict[str, Any]) -> dict[str, Any]:
    ids = {s["spanID"] for s in trace["spans"]}
    roots = [s for s in trace["spans"] if not any(r["spanID"] in ids for r in s.get("references", []))]
    root = min(roots or trace["spans"], key=lambda s: s["startTime"])
    return {"service": _service(trace, root), "operation": root["operationName"]}


def _error_spans(trace: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for span in trace["spans"]:
        tags = _tags(span)
        if _is_error(tags):
            out.append(
                {
                    "service": _service(trace, span),
                    "operation": span["operationName"],
                    "message": clip(_message(span, tags), 300),
                    "status": _status(tags),
                }
            )
    return out


# --- summaries -------------------------------------------------------------------------------


def summarize_search(raw: list[dict[str, Any]], args: TracesSearchArgs) -> dict[str, Any]:
    durations = [_duration_ms(t) for t in raw]
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    services: Counter[str] = Counter()
    with_errors = 0
    for trace in raw:
        for span in trace["spans"]:
            services[_service(trace, span)] += 1
        errors = _error_spans(trace)
        with_errors += bool(errors)
        for e in errors:
            key = (e["service"], e["operation"], e["message"])
            g = groups.setdefault(key, {**e, "count": 0, "example_trace_id": trace["traceID"]})
            g["count"] += 1
    slowest = sorted(raw, key=_duration_ms, reverse=True)[:5]
    out: dict[str, Any] = {
        "traces": len(raw),
        "hit_limit": len(raw) >= args.limit,
        "with_errors": with_errors,
        "duration_ms": {
            "p50": num(percentile(durations, 50)),
            "p95": num(percentile(durations, 95)),
            "max": num(max(durations, default=None)),
        },
        "error_spans": sorted(groups.values(), key=lambda g: (-g["count"], g["service"], g["operation"]))[:TOP],
        "slowest": [
            {
                "trace_id": t["traceID"],
                "duration_ms": num(_duration_ms(t)),
                "spans": len(t["spans"]),
                "root": _root(t),
                "error": bool(_error_spans(t)),
            }
            for t in slowest
        ],
        "spans_by_service": dict(services.most_common(TOP)),
    }
    if not raw:
        out["note"] = "no traces matched in this window"
    return out


def summarize_trace(raw: dict[str, Any], args: TraceGetArgs) -> dict[str, Any]:
    spans = raw["spans"]
    children: dict[str, int] = {}
    for s in spans:
        for r in s.get("references", []):
            if r.get("refType") == "CHILD_OF":
                children[r["spanID"]] = children.get(r["spanID"], 0) + s["duration"]
    rows = []
    for s in spans:
        tags = _tags(s)
        self_us = max(0, s["duration"] - children.get(s["spanID"], 0))
        rows.append(
            {
                "service": _service(raw, s),
                "operation": s["operationName"],
                "duration_ms": num(s["duration"] / 1000),
                "self_ms": num(self_us / 1000),
                "error": _is_error(tags),
            }
        )
    rows.sort(key=lambda r: -(r["self_ms"] or 0))
    return {
        "trace_id": raw["traceID"],
        "spans": len(spans),
        "duration_ms": num(_duration_ms(raw)),
        "root": _root(raw),
        "services": sorted({_service(raw, s) for s in spans}),
        "errors": _error_spans(raw)[:TOP],
        # Where the time went: spans by their own time, excluding time spent in children.
        "slowest_spans": rows[:TOP],
    }


TRACES_SEARCH = Tool(
    name="traces_search",
    description=(
        "Find recent traces through a service (Jaeger). Returns trace duration percentiles, error spans "
        "grouped by service, operation and message with an example trace id, and the slowest traces."
    ),
    args=TracesSearchArgs,
    backend="jaeger",
    fetch=_search,
    summarize=summarize_search,
    timeout_s=15.0,
)

TRACE_GET = Tool(
    name="trace_get",
    description=(
        "One trace by id: its services, error spans with messages, and the spans that took the most time "
        "themselves (excluding their children)."
    ),
    args=TraceGetArgs,
    backend="jaeger",
    fetch=_get,
    summarize=summarize_trace,
)
