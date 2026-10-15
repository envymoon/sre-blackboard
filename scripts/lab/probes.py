"""M3 acceptance: during each fault, call the read-only tools and check they see it.

For every fault the acceptance run injects, after the alerts have settled and before recovery,
a fixed set of tool calls runs against the live lab. Each call has a check on its summary that
only passes if the tool returned the data a diagnosis needs (the error ratio on the right
service, the bad revision and its diff, the slow spans...). Every answer is recorded to a
cassette per fault, which is what later lets tests and the agent run without a cluster.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings
from app.tools import ALL_TOOLS, ToolContext, ToolOutcome, ToolRegistry
from app.tools.clients import Clients
from app.tools.evidence import MemoryEvidenceStore
from app.tools.sources import Cassette, LiveSource, RecordingSource
from scripts.lab.faults import Fault

Summary = dict[str, Any]
# A check returns (passed, detail); the detail goes into the acceptance log either way.
Check = Callable[[Summary], tuple[bool, str]]
Args = dict[str, Any] | Callable[[dict[str, ToolOutcome]], dict[str, Any]]

ERROR_RATIO = "service:sli_error_ratio:rate5m"
P95 = "service:sli_latency_p95_ms:rate5m"


@dataclass(frozen=True)
class Probe:
    name: str
    tool: str
    args: Args
    check: Check


@dataclass
class ProbeResult:
    name: str
    tool: str
    args: dict[str, Any]
    ok: bool
    detail: str
    evidence_id: str | None = None


# --- checks ----------------------------------------------------------------------------------


def _value(summary: Summary, service: str) -> float | None:
    for s in summary.get("top", []):
        if s["labels"].get("service_name") == service:
            value: float | None = s["value"]
            return value
    return None


def above(service: str, threshold: float) -> Check:
    def check(s: Summary) -> tuple[bool, str]:
        v = _value(s, service)
        return v is not None and v > threshold, f"{service}={v} (want > {threshold})"

    return check


def rose(service: str, threshold: float) -> Check:
    """A range result whose series for ``service`` started below and peaked above ``threshold``."""

    def check(s: Summary) -> tuple[bool, str]:
        for series in s.get("series", []):
            if series["labels"].get("service_name") == service:
                ok = series["first"] < threshold < series["max"]
                return ok, f"{service}: first={series['first']} max={series['max']} peak_at={series['peak_at']}"
        return False, f"no series for {service}"

    return check


def flag_on(flag: str) -> Check:
    def check(s: Summary) -> tuple[bool, str]:
        on = [f["flag"] for f in s.get("on", [])]
        return flag in on, f"on: {on}"

    return check


def has_lines(pattern: str | None = None) -> Check:
    def check(s: Summary) -> tuple[bool, str]:
        groups = s.get("groups", [])
        top = groups[0]["template"] if groups else None
        if pattern is None:
            return s.get("lines", 0) > 0, f"{s.get('lines')} lines, top: {top}"
        hit = next((g for g in groups if re.search(pattern, str(g), re.I)), None)
        return hit is not None, f"{s.get('lines')} lines, match: {hit['template'] if hit else None}, top: {top}"

    return check


def error_spans_in(service: str) -> Check:
    def check(s: Summary) -> tuple[bool, str]:
        spans = [e for e in s.get("error_spans", []) if e["service"] == service]
        first = f"{spans[0]['operation']}: {spans[0]['message']}" if spans else None
        return bool(spans), f"{s.get('with_errors')}/{s.get('traces')} traces with errors; {service}: {first}"

    return check


def slow_traces(min_ms: float) -> Check:
    def check(s: Summary) -> tuple[bool, str]:
        p95 = s.get("duration_ms", {}).get("p95")
        return s.get("traces", 0) > 0 and (p95 or 0) >= min_ms, f"{s.get('traces')} traces, p95={p95}ms"

    return check


def self_time_in(service: str, min_ms: float) -> Check:
    def check(s: Summary) -> tuple[bool, str]:
        top = s.get("slowest_spans", [{}])[0]
        mine = [r for r in s.get("slowest_spans", []) if r["service"] == service]
        ok = bool(mine) and (mine[0]["self_ms"] or 0) >= min_ms
        return ok, f"slowest: {top.get('service')} {top.get('operation')} self={top.get('self_ms')}ms"

    return check


def no_chaos_events(s: Summary) -> tuple[bool, str]:
    leaked = [g["object"] for g in s.get("groups", []) if "Chaos" in g["object"]]
    return not leaked, f"{s.get('events')} events, by reason {s.get('by_reason')}; chaos objects: {leaked}"


def event_reason(reason: str) -> Check:
    def check(s: Summary) -> tuple[bool, str]:
        return reason in s.get("by_reason", {}), f"by reason: {s.get('by_reason')}"

    return check


def current_cause(pattern: str) -> Check:
    def check(s: Summary) -> tuple[bool, str]:
        current: dict[str, Any] = next((r for r in s.get("revisions", []) if r["current"]), {})
        cause = current.get("change_cause") or ""
        return bool(re.search(pattern, cause)), f"revision {current.get('revision')}: {cause!r}"

    return check


def changed(path_suffix: str, to: str) -> Check:
    def check(s: Summary) -> tuple[bool, str]:
        hit = [c for c in s.get("changes", []) if c["path"].endswith(path_suffix)]
        ok = bool(hit) and hit[0]["to"] == to
        return ok, f"{len(s.get('changes', []))} changes; {hit[0] if hit else 'no ' + path_suffix}"

    return check


def _slowest_trace(prior: dict[str, ToolOutcome]) -> dict[str, Any]:
    summary = prior["slow_traces"].summary or {}
    slowest = summary.get("slowest") or [{}]
    return {"trace_id": slowest[0].get("trace_id", "0" * 32)}


# --- per fault -------------------------------------------------------------------------------

PROBES: dict[str, list[Probe]] = {
    "payment-failure": [
        Probe("error_ratio", "prom_query", {"query": ERROR_RATIO}, above("payment", 0.1)),
        Probe("error_trend", "prom_range", {"query": ERROR_RATIO, "minutes": 15}, rose("payment", 0.1)),
        Probe("flags", "flag_state", {}, flag_on("paymentFailure")),
        Probe(
            "error_traces",
            "traces_search",
            {"service": "payment", "errors_only": True, "minutes": 10},
            error_spans_in("payment"),
        ),
        Probe("logs", "logs_search", {"service": "payment", "minutes": 10}, has_lines()),
    ],
    "network-delay": [
        Probe("p95", "prom_query", {"query": P95}, above("product-catalog", 1000)),
        Probe(
            "slow_traces",
            "traces_search",
            {"service": "product-catalog", "min_duration_ms": 900, "minutes": 10},
            slow_traces(900),
        ),
        Probe("slow_trace", "trace_get", _slowest_trace, self_time_in("product-catalog", 500)),
        Probe("events", "k8s_events", {"minutes": 30}, no_chaos_events),
        Probe("flags", "flag_state", {}, lambda s: (not s.get("on"), f"on: {s.get('on')}")),
    ],
    "bad-release": [
        Probe("error_ratio", "prom_query", {"query": ERROR_RATIO}, above("checkout", 0.1)),
        Probe("history", "rollout_history", {"deployment": "checkout"}, current_cause("port 9999")),
        Probe("diff", "deploy_diff", {"deployment": "checkout"}, changed("env.PAYMENT_ADDR", "payment:9999")),
        Probe("events", "k8s_events", {"object_name": "checkout", "minutes": 30}, event_reason("ScalingReplicaSet")),
        Probe(
            "error_traces",
            "traces_search",
            {"service": "checkout", "errors_only": True, "minutes": 10},
            error_spans_in("checkout"),
        ),
        Probe(
            "error_logs",
            "logs_search",
            {"service": "checkout", "level": "error", "minutes": 10},
            has_lines(r"9999|refused|unavailable"),
        ),
    ],
}


class ProbeRunner:
    """Called by the acceptance run while a fault is active. Records one cassette per fault."""

    def __init__(
        self,
        settings: Settings,
        out_dir: Path,
        tenant: str = "lab",
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.out_dir = out_dir
        self.tenant = tenant
        self.transport = transport
        self.now = now

    def __call__(self, fault: Fault) -> list[ProbeResult]:
        probes = PROBES.get(fault.name, [])
        return asyncio.run(self._run(fault, probes)) if probes else []

    async def _run(self, fault: Fault, probes: list[Probe]) -> list[ProbeResult]:
        cassette = Cassette(meta={"fault": fault.name, "root_cause": fault.root_cause, "symptoms": fault.symptoms})
        clients = Clients.from_settings(self.settings, transport=self.transport)
        live = LiveSource(clients, now=self.now)
        registry = ToolRegistry(ALL_TOOLS, RecordingSource(live, cassette), MemoryEvidenceStore())
        ctx = ToolContext(tenant_id=self.tenant, scope=f"lab/{fault.name}")
        prior: dict[str, ToolOutcome] = {}
        results = []
        try:
            for probe in probes:
                args = probe.args(prior) if callable(probe.args) else probe.args
                outcome = await registry.call(probe.tool, args, ctx)
                prior[probe.name] = outcome
                if outcome.error is not None:
                    ok, detail = False, f"error {outcome.error.code}: {outcome.error.message}"
                else:
                    ok, detail = probe.check(outcome.summary or {})
                results.append(ProbeResult(probe.name, probe.tool, outcome.args, ok, detail, outcome.evidence_id))
        finally:
            await clients.aclose()
            cassette.dump(self.out_dir / f"{fault.name}.jsonl")
        return results
