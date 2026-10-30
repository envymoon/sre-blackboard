"""M8 acceptance in CI, without a model: the system's metrics, alerts, dashboard and traces,
checked against the real Prometheus, Grafana and Jaeger of ``docker compose --profile observability``.

Run after the load test (scripts/loadtest.py) against the same stack:

- Prometheus scrapes the API and every worker, and what they report adds up: every alert the
  load test sent, every incident, every triage job; nothing left waiting in the queue.
- Every alert rule is loaded and healthy, and every dashboard query evaluates without an error
  (a typo in PromQL fails here, not in front of someone on call).
- Grafana has the dashboard and both data sources answer.
- Jaeger: an incident opened by the API and triaged by a worker is one trace. The context
  crossed processes through Postgres (incidents.trace_context, jobs.trace_context).
- ``--replay``: a recorded investigation (tests/fixtures/agent/bad-release) is replayed with
  its spans exported to the same Jaeger, and the trace has the whole tree: jobs, agents, model
  calls with their token counts, tool calls, the policy.

Exits non-zero on the first failed check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

REPO = Path(__file__).resolve().parents[1]


def ok(msg: str) -> None:
    print(f"ok    {msg}")


def fail(msg: str) -> None:
    print(f"FAIL  {msg}")
    sys.exit(1)


def wait_for(what: str, check: Callable[[], Any], timeout_s: float = 120) -> Any:
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            last = repr(e)
        time.sleep(3)
    fail(f"{what} (last: {last})")


class Prometheus:
    def __init__(self, url: str) -> None:
        self.http = httpx.Client(base_url=url, timeout=10)

    def query(self, expr: str) -> list[dict[str, Any]]:
        body = self.http.get("/api/v1/query", params={"query": expr}).json()
        if body.get("status") != "success":
            raise ValueError(f"{expr}: {body.get('error')}")
        return list(body["data"]["result"])

    def scalar(self, expr: str) -> float | None:
        result = self.query(expr)
        return float(result[0]["value"][1]) if result else None


def check_prometheus(prom: Prometheus, alerts: int, distinct: int, workers: int) -> None:
    def targets_up() -> bool:
        up = {r["metric"]["job"]: float(r["value"][1]) for r in prom.query('sum by (job) (up{job=~"sre-.*"})')}
        return up.get("sre-api") == 1 and up.get("sre-worker") == workers

    wait_for(f"the API and {workers} workers are scraped and up", targets_up)
    ok(f"targets up: sre-api, {workers} sre-worker")

    expected = {
        "sum(sre_alerts_received_total)": alerts,
        'sum(sre_alerts_received_total{result="opened"})': distinct,
        "sum(sre_incidents_opened_total)": distinct,
        'sum(sre_jobs_total{kind="triage", outcome="done"})': distinct,
        'sum(sre_incident_transitions_total{from_state="RECEIVED", to_state="TRIAGED"})': distinct,
        'sum(sre_queue_wait_seconds_count{kind="triage"})': distinct,
        'sum(max by (status) (sre_queue_jobs{status="queued"})) or vector(0)': 0,
        "max(sre_queue_ready_oldest_seconds)": 0,
        "max(sre_state_up)": 1,
        'sum(max by (state) (sre_incidents_open{state="TRIAGED"}))': distinct,
    }
    for expr, want in expected.items():
        wait_for(f"{expr} == {want}", partial(_equals, prom, expr, want), timeout_s=90)
        ok(f"{expr} = {want}")


def _equals(prom: Prometheus, expr: str, want: float) -> bool:
    return prom.scalar(expr) == want


def exprs(source: str) -> list[str]:
    import yaml

    out = []
    if source == "rules":
        for path in sorted((REPO / "deploy/observability/rules").glob("*.yml")):
            for group in yaml.safe_load(path.read_text())["groups"]:
                out += [r["expr"] for r in group["rules"]]
    else:
        for path in sorted((REPO / "deploy/observability/grafana/dashboards").glob("*.json")):
            for panel in json.loads(path.read_text())["panels"]:
                out += [t["expr"].replace("$__rate_interval", "1m") for t in panel.get("targets", [])]
    return out


def check_queries(prom: Prometheus) -> None:
    rules = prom.http.get("/api/v1/rules").json()["data"]["groups"]
    loaded = [r for g in rules for r in g["rules"]]
    unhealthy = [r["name"] for r in loaded if r.get("health") not in ("ok", "unknown")]
    if len(loaded) != len(exprs("rules")) or unhealthy:
        fail(f"rules: {len(loaded)} loaded of {len(exprs('rules'))}, unhealthy: {unhealthy}")
    ok(f"{len(loaded)} alert and recording rules loaded, all healthy")
    for source in ("rules", "dashboard"):
        bad = []
        for e in exprs(source):
            try:
                prom.query(e)
            except ValueError as err:
                bad.append(str(err))
        if bad:
            fail(f"{source} queries Prometheus rejects: {bad}")
        ok(f"every {source} query evaluates ({len(exprs(source))})")


def check_grafana(url: str) -> None:
    http = httpx.Client(base_url=url, timeout=10)
    wait_for("Grafana is up", lambda: http.get("/api/health").json().get("database") == "ok")
    board = wait_for(
        "the dashboard is provisioned",
        lambda: http.get("/api/dashboards/uid/sre-blackboard").json().get("dashboard"),
    )
    ok(f"Grafana dashboard {board['title']!r}: {len(board['panels'])} panels")
    for uid in ("sre-prometheus", "sre-jaeger"):
        health = http.get(f"/api/datasources/uid/{uid}/health").json()
        if health.get("status") != "OK":
            fail(f"data source {uid}: {health}")
        ok(f"data source {uid} answers")


class Jaeger:
    def __init__(self, url: str) -> None:
        self.http = httpx.Client(base_url=url, timeout=10)

    def traces(self, service: str, operation: str, limit: int = 20) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {"service": service, "operation": operation, "limit": limit, "lookback": "1h"}
        return list(self.http.get("/api/traces", params=params).json().get("data") or [])


def tree(trace: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Spans by id, each with its service and parent id."""
    spans = {}
    for s in trace["spans"]:
        parent = next((r["spanID"] for r in s.get("references", []) if r["refType"] == "CHILD_OF"), None)
        service = trace["processes"][s["processID"]]["serviceName"]
        spans[s["spanID"]] = {**s, "service": service, "parent": parent}
    return spans, {sid: s["operationName"] for sid, s in spans.items()}


def check_ingest_trace(jaeger: Jaeger) -> None:
    def found() -> Any:
        for t in jaeger.traces("sre-blackboard-worker", "job triage"):
            spans, _ = tree(t)
            roots = [s for s in spans.values() if s["parent"] is None]
            jobs = [s for s in spans.values() if s["operationName"] == "job triage"]
            if (
                len(roots) == 1
                and roots[0]["service"] == "sre-blackboard-api"
                and roots[0]["operationName"].startswith("incident ")
                and jobs
                and all(j["parent"] == roots[0]["spanID"] for j in jobs)
            ):
                return t
        return None

    t = wait_for("a trace whose root the API opened and whose triage job a worker ran", found, timeout_s=60)
    spans, _ = tree(t)
    ok(f"trace {t['traceID']}: {sorted({s['service'] for s in spans.values()})}, incident root -> job triage")


async def replay_into(database_url: str, otel_endpoint: str) -> str:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import Settings
    from app.eval import replay
    from app.obs import metrics, tracing

    shutdown = tracing.setup("sre-blackboard-replay", Settings(_env_file=None, otel_endpoint=otel_endpoint))
    engine = create_async_engine(database_url)
    metrics.install(engine)
    try:
        r = await replay.replay(engine, "bad-release", "default", REPO / "tests/fixtures/agent/bad-release")
    finally:
        await engine.dispose()
        shutdown()
    if r.llm_misses or r.tool_misses:
        fail(f"replay misses: {r.llm_misses} model, {r.tool_misses} tool")
    [inc] = r.observation.incidents
    return inc.incident_id


def check_replay_trace(jaeger: Jaeger, incident_id: str) -> None:
    def found() -> Any:
        for t in jaeger.traces("sre-blackboard-replay", "job investigate"):
            spans, _ = tree(t)
            root = next((s for s in spans.values() if s["parent"] is None), None)
            tags = {tg["key"]: tg["value"] for tg in (root or {}).get("tags", [])}
            if tags.get("sre.incident.id") == incident_id:
                return t
        return None

    t = wait_for(f"the replayed incident {incident_id}'s trace", found, timeout_s=60)
    spans, names = tree(t)

    def under(prefix: str, parent_prefix: str) -> list[dict[str, Any]]:
        return [
            s
            for s in spans.values()
            if s["operationName"].startswith(prefix) and names.get(s["parent"] or "", "").startswith(parent_prefix)
        ]

    need = {
        "job triage / investigate / plan under the root": len(under("job ", "incident ")) == 3,
        "agents under job investigate": len(under("invoke_agent ", "job investigate")) >= 3,
        "the planner under job plan": len(under("invoke_agent planner", "job plan")) == 1,
        "the policy under job plan": len(under("policy.evaluate", "job plan")) == 1,
        "model calls under agents": len(under("chat ", "invoke_agent ")) >= 5,
        "tool calls under agents": len(under("execute_tool ", "invoke_agent ")) >= 3,
    }
    missing = [k for k, v in need.items() if not v]
    if missing:
        fail(f"trace {t['traceID']} lacks: {missing}")
    chat = under("chat ", "invoke_agent ")[0]
    tags = {tg["key"]: tg["value"] for tg in chat["tags"]}
    for key in ("gen_ai.operation.name", "gen_ai.request.model", "gen_ai.usage.input_tokens", "sre.llm.request_hash"):
        if key not in tags:
            fail(f"chat span without {key}: {sorted(tags)}")
    counts: dict[str, int] = {}
    for s in spans.values():
        kind = s["operationName"].split(" ")[0]
        counts[kind] = counts.get(kind, 0) + 1
    ok(f"replayed trace {t['traceID']}: {len(spans)} spans {counts}")
    ok(f"open it: {jaeger.http.base_url}/trace/{quote(t['traceID'])}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--prometheus", default="http://localhost:9091")
    p.add_argument("--grafana", default="http://localhost:3000")
    p.add_argument("--jaeger", default="http://localhost:16686")
    p.add_argument("--alerts", type=int, default=2000)
    p.add_argument("--distinct", type=int, default=200)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--replay", metavar="DATABASE_URL", help="an empty, migrated database to replay into")
    p.add_argument("--otlp", default="http://localhost:4318")
    args = p.parse_args()

    prom = Prometheus(args.prometheus)
    check_prometheus(prom, args.alerts, args.distinct, args.workers)
    check_queries(prom)
    check_grafana(args.grafana)
    jaeger = Jaeger(args.jaeger)
    check_ingest_trace(jaeger)
    if args.replay:
        incident_id = asyncio.run(replay_into(args.replay, args.otlp))
        check_replay_trace(jaeger, incident_id)
    print("all observability checks passed")


if __name__ == "__main__":
    main()
