"""The system's own observability (M8, ADR-0011): traces, metrics, trace-correlated logs."""

from __future__ import annotations

import logging
import re
from collections import Counter as Tally
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db import incidents
from app.eval import replay
from app.obs import metrics, tracing
from app.remediation import flow
from tests.test_api import auth

FIXTURES = Path(__file__).parent / "fixtures" / "agent"
REPO = Path(__file__).resolve().parents[1]
OBS = REPO / "deploy" / "observability"

_EXPORTER = InMemorySpanExporter()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """Record spans in memory. The global provider can be set once per process: the first
    test that asks for it sets it, later ones reuse it."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)
    _EXPORTER.clear()
    yield _EXPORTER
    _EXPORTER.clear()


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def by_name(spans: tuple[ReadableSpan, ...], prefix: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name.startswith(prefix)]


# --- traces ----------------------------------------------------------------------------


async def test_a_replayed_incident_is_one_trace_from_its_root_to_the_policy(
    engine: AsyncEngine, spans: InMemorySpanExporter
) -> None:
    llm_before = value(
        "sre_llm_requests_total", agent="coordinator", model="claude-haiku-5-5", backend="replay", outcome="ok"
    )
    r = await replay.replay(engine, "bad-release", "default", FIXTURES / "bad-release")
    assert (r.llm_misses, r.tool_misses) == (0, 0)
    [inc] = r.observation.incidents
    finished = spans.get_finished_spans()

    [root] = by_name(finished, "incident ")
    assert root.parent is None and root.attributes is not None
    assert root.attributes["sre.incident.id"] == inc.incident_id
    trace_id = root.context.trace_id
    assert {s.context.trace_id for s in finished} == {trace_id}, "every span is in the incident's trace"

    # Every job is a child of the root, whichever process enqueued it.
    jobs = {s.name: s for s in by_name(finished, "job ")}
    assert set(jobs) == {"job triage", "job investigate", "job plan"}
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id for s in jobs.values())

    def children(parent: ReadableSpan, prefix: str) -> list[ReadableSpan]:
        return [s for s in by_name(finished, prefix) if s.parent and s.parent.span_id == parent.context.span_id]

    agents = children(jobs["job investigate"], "invoke_agent ")
    assert {a.name for a in agents} >= {"invoke_agent coordinator", "invoke_agent synthesizer"}
    assert any(a.name.startswith("invoke_agent investigator.") for a in agents)
    [planner] = children(jobs["job plan"], "invoke_agent planner")
    [policy] = children(jobs["job plan"], "policy.evaluate")
    assert policy.attributes is not None and policy.attributes["sre.policy.verdict"] == "require_approval"

    # Model calls nest in their agent, with the GenAI attributes; tool calls too.
    chats = [c for a in [*agents, planner] for c in children(a, "chat ")]
    assert chats and all(c.attributes is not None for c in chats)
    first = chats[0].attributes
    assert first is not None
    assert first["gen_ai.operation.name"] == "chat" and first["gen_ai.provider.name"] == "anthropic"
    assert first["gen_ai.conversation.id"] == inc.incident_id
    assert sum(int(c.attributes["gen_ai.usage.input_tokens"]) for c in chats if c.attributes) > 0
    assert len(first["sre.llm.request_hash"]) == 64  # type: ignore[arg-type]
    tools = [t for a in agents for t in children(a, "execute_tool ")]
    assert tools and all(t.attributes and t.attributes["gen_ai.tool.call.arguments"] for t in tools)
    assert any(t.attributes and t.attributes.get("sre.evidence.id") for t in tools)

    # State changes are events on the span that made them.
    moves = [
        (e.attributes["from"], e.attributes["to"])
        for s in finished
        for e in s.events
        if e.name == "incident.transition" and e.attributes
    ]
    assert moves[:3] == [("RECEIVED", "TRIAGED"), ("TRIAGED", "INVESTIGATING"), ("INVESTIGATING", "DIAGNOSED")]
    assert moves[-1] == ("SAFETY_REVIEWED", "AWAITING_APPROVAL")

    # The same run, as metrics.
    assert (
        value("sre_llm_requests_total", agent="coordinator", model="claude-haiku-5-5", backend="replay", outcome="ok")
        > llm_before
    )
    assert value("sre_policy_decisions_total", stage="submit", verdict="require_approval", risk="R3") >= 1


async def test_an_approval_enqueues_into_the_incident_trace_not_the_request(
    engine: AsyncEngine, spans: InMemorySpanExporter
) -> None:
    r = await replay.replay(engine, "bad-release", "default", FIXTURES / "bad-release")
    [inc] = r.observation.incidents
    waited_before = value("sre_approval_wait_seconds_count", decision="approve")
    with tracing.TRACER.start_as_current_span("POST /approval"):  # stands in for the API request
        async with engine.begin() as conn:
            row = await incidents.get(conn, inc.incident_id, tenant_id=None)  # type: ignore[arg-type]
            plan = await flow.current_plan(conn, row["id"])  # type: ignore[index]
            assert plan is not None
            await flow.decide(
                conn,
                row,
                approver="alice",
                decision="approve",
                hash_seen=plan.plan_hash,
                comment="",
                settings=Settings(),
            )
    async with engine.connect() as conn:
        incident_ctx, job_ctx = (
            await conn.execute(
                text(
                    "SELECT i.trace_context, j.trace_context FROM incidents i JOIN jobs j ON j.incident_id = i.id "
                    "WHERE i.id = :id AND j.kind = 'execute'"
                ),
                {"id": inc.incident_id},
            )
        ).one()
    assert incident_ctx and job_ctx == incident_ctx
    assert value("sre_approval_wait_seconds_count", decision="approve") == waited_before + 1


async def test_without_a_tracer_nothing_is_stored() -> None:
    assert tracing.traceparent() is None or trace.get_current_span().get_span_context().is_valid
    assert tracing.context_of(None) is None
    assert tracing.trace_id_of("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01") == (
        "0af7651916cd43dd8448eb211c80319c"
    )
    assert tracing.trace_id_of("garbage") is None


def test_log_lines_carry_the_trace_id(spans: InMemorySpanExporter) -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    tracing.TraceIdFilter().filter(record)
    assert record.trace_id == "-"  # type: ignore[attr-defined]
    with tracing.TRACER.start_as_current_span("x") as span:
        tracing.TraceIdFilter().filter(record)
    assert record.trace_id == format(span.get_span_context().trace_id, "032x")  # type: ignore[attr-defined]


# --- metrics ---------------------------------------------------------------------------


async def test_a_state_change_that_rolls_back_is_not_counted(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        incident_id, _ = await incidents.open_or_merge(
            conn,
            tenant_id="t1",
            fingerprint="fp-rollback",
            title="A",
            service="s",
            environment="e",
            priority=0,
            labels={},
        )
    before = value("sre_incident_transitions_total", from_state="RECEIVED", to_state="TRIAGED")
    with pytest.raises(RuntimeError):
        async with engine.begin() as conn:
            await incidents.transition(
                conn, incident_id, from_state="RECEIVED", to_state="TRIAGED", expected_version=1, actor="test"
            )
            raise RuntimeError("something after the change failed")
    assert value("sre_incident_transitions_total", from_state="RECEIVED", to_state="TRIAGED") == before
    async with engine.begin() as conn:
        await incidents.transition(
            conn, incident_id, from_state="RECEIVED", to_state="TRIAGED", expected_version=1, actor="test"
        )
    assert value("sre_incident_transitions_total", from_state="RECEIVED", to_state="TRIAGED") == before + 1
    assert value("sre_incident_state_duration_seconds_count", state="RECEIVED", priority="P0") >= 1


async def test_the_api_reports_what_waits_in_the_database(
    client: httpx.AsyncClient, settings: Settings, spans: InMemorySpanExporter
) -> None:
    alert = {
        "status": "firing",
        "alerts": [
            {"status": "firing", "labels": {"alertname": "HighErrorRate", "service": "payment", "priority": "P0"}}
        ],
    }
    headers = auth(settings, "ingest")
    opened = value("sre_alerts_received_total", status="firing", result="opened")
    assert (await client.post("/api/v1/alerts", json=alert, headers=headers)).status_code == 202
    assert (await client.post("/api/v1/alerts", json=alert, headers=headers)).status_code == 202
    assert value("sre_alerts_received_total", status="firing", result="opened") == opened + 1

    body = (await client.get("/metrics")).text
    assert "sre_state_up 1.0" in body
    assert re.search(r'sre_queue_jobs\{kind="triage",priority="P0",status="queued"\} 1\.0', body)
    assert re.search(r'sre_incidents_open\{priority="P0",state="RECEIVED"\} 1\.0', body)
    assert re.search(r'sre_incidents_undiagnosed_oldest_seconds\{priority="P0"\} [0-9.]+', body)
    assert 'sre_incidents_undiagnosed_oldest_seconds{priority="P3"} 0.0' in body  # present, not absent
    assert 'sre_http_requests_total{method="POST",route="/api/v1/alerts",status="202"}' in body

    # The webhook request is a span; the incident it opened is a trace of its own, linked to it.
    finished = spans.get_finished_spans()
    requests = [s for s in finished if s.name == "POST /api/v1/alerts"]
    assert len(requests) == 2 and all(s.kind == SpanKind.SERVER for s in requests)
    [root] = by_name(finished, "incident HighErrorRate payment")
    assert root.context.trace_id not in {s.context.trace_id for s in requests}
    assert [link.context.trace_id for link in root.links] == [requests[0].context.trace_id]
    assert not [s for s in finished if s.name.startswith("GET /metrics")]  # the scrape is not traced


# --- the dashboard and the alert rules use only metrics that exist ---------------------

DEFINED = {
    m.name
    for m in REGISTRY.collect()  # type: ignore[no-untyped-call]
    if m.name.startswith("sre_")
} | {
    "sre_queue_jobs",
    "sre_queue_ready_oldest_seconds",
    "sre_incidents_open",
    "sre_incidents_undiagnosed_oldest_seconds",
    "sre_approvals_pending_oldest_seconds",
    "sre_state_up",
}
SUFFIXES = ("_total", "_bucket", "_count", "_sum", "_created")


def referenced(expr: str) -> set[str]:
    names = set(re.findall(r"\bsre_[a-z0-9_]+", expr))
    out = set()
    for n in names:
        for suffix in SUFFIXES:
            if n.endswith(suffix):
                n = n[: -len(suffix)]
                break
        out.add(n)
    return out


def dashboard_exprs() -> list[str]:
    import json

    exprs = []
    for path in (OBS / "grafana" / "dashboards").glob("*.json"):
        for panel in json.loads(path.read_text())["panels"]:
            exprs += [t["expr"] for t in panel.get("targets", []) if "expr" in t]
    return exprs


def rule_exprs() -> list[str]:
    exprs = []
    for path in (OBS / "rules").glob("*.yml"):
        for group in yaml.safe_load(path.read_text())["groups"]:
            exprs += [r["expr"] for r in group["rules"]]
    return exprs


@pytest.mark.parametrize("source", ["dashboard", "rules"])
def test_dashboards_and_alerts_only_use_metrics_the_code_defines(source: str) -> None:
    exprs = dashboard_exprs() if source == "dashboard" else rule_exprs()
    assert exprs
    unknown = Tally(n for e in exprs for n in referenced(e) if n not in DEFINED)
    assert not unknown, f"not defined in app/obs: {sorted(unknown)}"


def test_every_metric_is_on_the_dashboard() -> None:
    shown = {n for e in dashboard_exprs() for n in referenced(e)}
    missing = sorted(DEFINED - shown)
    assert not missing, f"defined but not on the dashboard: {missing}"


def test_the_dashboard_json_is_generated_from_its_script() -> None:
    from scripts import dashboard

    assert dashboard.OUT.read_text() == dashboard.render(), "run: uv run python -m scripts.dashboard"


def test_metric_names_follow_the_conventions() -> None:
    for m in REGISTRY.collect():  # type: ignore[no-untyped-call]
        if m.name.startswith("sre_") and m.type == "histogram":
            assert m.name.endswith(("_seconds", "_steps")), m.name
    assert metrics.STAGE_BUCKETS.count(120) == 1  # the P0 objective is a bucket boundary
