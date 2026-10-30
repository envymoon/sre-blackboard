"""Prometheus metrics (M8, ADR-0011): what the system does, per process.

Counters and histograms are observed where things happen: the queue and the worker (waits,
job outcomes), the state machine (time in each state, time to the first diagnosis), the agent
loop (model calls, tokens, cost), the tool registry (latency, errors), the policy, approvals,
the executor and the verifier. What is *in* the database right now (queue depth, the oldest
undiagnosed incident) is read when the API is scraped instead (app/obs/state.py), so it is
still reported when every worker is down, which is exactly when it matters.

Labels are bounded: kinds, states, priorities, agent roles, tool names, models, error codes.
No tenant, incident or service label: those are in the trace (app/obs/tracing.py), and a
histogram exemplar carries the trace id where one exists, so a slow bucket leads to a trace.

Observations made inside a database transaction wait for it to commit (``after_commit``): a
state change that is rolled back (a stale version, a lost lease) is not counted.

The API runs several processes: there ``PROMETHEUS_MULTIPROC_DIR`` is set and the values are
summed across them on scrape (app/api/metrics.py). Workers and the executor are one process
each and serve their own registry on ``SRE_METRICS_PORT``.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from typing import Any

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, multiprocess, start_http_server
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger(__name__)

# Incident stages take minutes; 120 s is the P0 first-diagnosis objective (deploy/observability/rules).
STAGE_BUCKETS = (5, 15, 30, 60, 90, 120, 180, 300, 600, 900, 1800, 3600, 7200)
FAST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
LLM_BUCKETS = (0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 180, 300)
JOB_BUCKETS = (0.05, 0.25, 1, 5, 15, 30, 60, 120, 300, 600, 1200)
QUEUE_BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

# Ingest and the state machine
ALERTS = Counter("sre_alerts_received", "Alerts received, by status and what became of them", ["status", "result"])
INCIDENTS_OPENED = Counter("sre_incidents_opened", "Incidents opened", ["priority"])
TRANSITIONS = Counter("sre_incident_transitions", "Incident state changes", ["from_state", "to_state"])
STATE_SECONDS = Histogram(
    "sre_incident_state_duration_seconds",
    "Time an incident spent in a state, observed when it leaves it",
    ["state", "priority"],
    buckets=STAGE_BUCKETS,
)
DIAGNOSIS_SECONDS = Histogram(
    "sre_incident_diagnosis_seconds",
    "Time from opening an incident to its first diagnosis (or escalation instead of one)",
    ["priority", "outcome"],
    buckets=STAGE_BUCKETS,
)
CLOSE_SECONDS = Histogram(
    "sre_incident_close_seconds",
    "Time from opening an incident to a final state",
    ["priority", "state"],
    buckets=STAGE_BUCKETS,
)

# Queue and workers
QUEUE_WAIT = Histogram(
    "sre_queue_wait_seconds",
    "Time a job was ready to run before a worker claimed it",
    ["kind", "priority"],
    buckets=QUEUE_BUCKETS,
)
JOBS = Counter("sre_jobs", "Job attempts, by how they ended", ["kind", "outcome"])
JOB_SECONDS = Histogram(
    "sre_job_duration_seconds", "Duration of one job attempt", ["kind", "outcome"], buckets=JOB_BUCKETS
)
JOBS_RUNNING = Gauge("sre_worker_jobs_running", "Jobs this process is running", multiprocess_mode="livesum")
WORKER_SLOTS = Gauge("sre_worker_slots", "Jobs this process may run at once", multiprocess_mode="livesum")

# Agents and the model
LLM_REQUESTS = Counter(
    "sre_llm_requests", "Model calls (one agent step each), by outcome", ["agent", "model", "backend", "outcome"]
)
LLM_TOKENS = Counter("sre_llm_tokens", "Tokens, by kind", ["agent", "model", "type"])
LLM_COST = Counter(
    "sre_llm_cost_usd", "Equivalent API cost the backend reported (the CLI reports it)", ["agent", "model"]
)
LLM_SECONDS = Histogram(
    "sre_llm_request_duration_seconds", "Duration of one model call", ["model", "backend"], buckets=LLM_BUCKETS
)
AGENT_RUNS = Counter("sre_agent_runs", "Agent runs, by outcome", ["agent", "outcome"])
AGENT_STEPS = Histogram(
    "sre_agent_steps", "Steps an agent took", ["agent"], buckets=(1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20)
)
AGENT_DROPPED = Counter("sre_agent_findings_dropped", "Findings dropped for failing the citation checks", ["agent"])
TOOL_CALLS = Counter("sre_tool_calls", "Tool calls, by outcome (ok or the error code)", ["tool", "outcome"])
TOOL_SECONDS = Histogram("sre_tool_duration_seconds", "Duration of one tool call", ["tool"], buckets=FAST_BUCKETS)

# Remediation
POLICY = Counter(
    "sre_policy_decisions", "Policy verdicts, at submission and again before execution", ["stage", "verdict", "risk"]
)
APPROVAL_WAIT = Histogram(
    "sre_approval_wait_seconds",
    "Time a plan waited for a human decision",
    ["decision"],
    buckets=(10, 30, 60, 120, 300, 600, 900, 1800, 3600),
)
EXECUTIONS = Counter("sre_executions", "Remediation actions run by the executor", ["action", "outcome"])
VERIFICATIONS = Counter("sre_verifications", "Repairs checked against the SLO", ["outcome"])

# The API
HTTP_REQUESTS = Counter("sre_http_requests", "API requests", ["method", "route", "status"])
HTTP_SECONDS = Histogram(
    "sre_http_request_duration_seconds", "API request duration", ["method", "route"], buckets=FAST_BUCKETS
)


def serve(port: int) -> None:
    """Expose this process's registry on ``port`` (0: not at all). A port that is taken is
    logged, not fatal: a worker without metrics is better than no worker."""
    if not port:
        return
    try:
        start_http_server(port)
    except OSError as e:
        log.warning("metrics not served on port %d: %s", port, e)


def exemplar() -> dict[str, str] | None:
    """The current trace id, to attach to a histogram observation."""
    ctx = trace.get_current_span().get_span_context()
    return {"trace_id": format(ctx.trace_id, "032x")} if ctx.is_valid else None


def observe(histogram: Histogram, value: float, **labels: str) -> None:
    histogram.labels(**labels).observe(value, exemplar=exemplar())


def registry() -> CollectorRegistry:
    """What this process exposes: the multiprocess sum under the API, the default registry elsewhere."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        merged = CollectorRegistry()
        multiprocess.MultiProcessCollector(merged)
        return merged
    from prometheus_client import REGISTRY

    return REGISTRY


# --- Observations that wait for the transaction --------------------------------------------

_PENDING = "sre_metrics_pending"


def after_commit(conn: AsyncConnection | None, fn: Callable[[], Any]) -> None:
    """Run ``fn`` when ``conn``'s transaction commits; drop it if it rolls back. Outside a
    transaction it runs now."""
    if conn is None or not conn.in_transaction() or not event.contains(conn.sync_engine, "commit", _flush):
        fn()  # no transaction, or an engine without the hook (install): nothing would ever run it
        return
    conn.sync_connection.info.setdefault(_PENDING, []).append(fn)  # type: ignore[union-attr]


def install(engine: AsyncEngine) -> None:
    """Hook ``after_commit`` into an engine (app/db/engine.py does it for every engine)."""
    sync = engine.sync_engine
    if event.contains(sync, "commit", _flush):
        return
    event.listen(sync, "commit", _flush)
    event.listen(sync, "rollback", _drop)
    event.listen(sync.pool, "checkin", _drop_record)


def _flush(conn: Any) -> None:
    for fn in conn.info.pop(_PENDING, []):
        with contextlib.suppress(Exception):  # a metric must never fail a commit
            fn()


def _drop(conn: Any) -> None:
    conn.info.pop(_PENDING, None)


def _drop_record(_dbapi_conn: Any, record: Any) -> None:
    record.info.pop(_PENDING, None)
