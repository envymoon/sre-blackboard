"""Traces (M8, ADR-0011): one trace per incident, across the API, the workers and the executor.

    incident HighErrorRate payment           root: the API, when an alert opens the incident
    ├── job triage                           a worker
    ├── job investigate
    │   ├── invoke_agent coordinator
    │   │   └── chat claude-haiku-5-5        gen_ai.*: model, tokens (OpenTelemetry GenAI conventions)
    │   ├── invoke_agent investigator.metrics
    │   │   ├── chat claude-haiku-5-5
    │   │   └── execute_tool prom_query      the arguments, the evidence id, the error code
    │   └── invoke_agent synthesizer
    ├── job plan
    │   ├── invoke_agent planner
    │   └── policy.evaluate                  verdict, risk, the checks that failed
    ├── job execute                          the executor
    │   └── remediation.action rollback payment
    └── job verify

The queue is the transport. The root span's context (a W3C traceparent) is stored with the
incident, and every job enqueued for the incident copies it (app/queue/jobs.py), so whichever
process claims the job continues the incident's trace. The root span ends as soon as it is
opened; its children run for minutes after it, in other processes.

Nothing is exported without ``SRE_OTEL_ENDPOINT``: the tracer is then OpenTelemetry's no-op one
and no trace context is stored. Prompts and model answers are not put on spans: they are in
``llm_calls``, joined by the request hash on every ``chat`` span.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app import __version__

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.queue.jobs import Job

TRACER = trace.get_tracer("sre-blackboard", __version__)
_PROPAGATOR = TraceContextTextMapPropagator()
PRIORITIES = ("P0", "P1", "P2", "P3")

# OpenTelemetry GenAI semantic conventions (development status; the names the SDK's
# opentelemetry.semconv._incubating module uses).
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_PROVIDER = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_CONVERSATION = "gen_ai.conversation.id"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_ARGS = "gen_ai.tool.call.arguments"
ERROR_TYPE = "error.type"


def setup(service_name: str, settings: Settings, *, instance: str | None = None) -> Callable[[], None]:
    """Export this process's spans over OTLP/HTTP, if an endpoint is configured. Returns the
    shutdown function (it flushes what is still buffered)."""
    if not settings.otel_endpoint:
        return lambda: None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    attributes = {"service.name": service_name, "service.namespace": "sre-blackboard", "service.version": __version__}
    if instance:
        attributes["service.instance.id"] = instance
    provider = TracerProvider(resource=Resource.create(attributes))
    endpoint = settings.otel_endpoint.rstrip("/") + "/v1/traces"
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5)))
    trace.set_tracer_provider(provider)
    return provider.shutdown


def traceparent(span: Span | None = None) -> str | None:
    """The W3C traceparent of ``span`` (default: the current one), or None when not recording."""
    carrier: dict[str, str] = {}
    ctx = trace.set_span_in_context(span) if span is not None else None
    _PROPAGATOR.inject(carrier, context=ctx)
    return carrier.get("traceparent")


def context_of(value: str | None) -> otel_context.Context | None:
    return _PROPAGATOR.extract({"traceparent": value}) if value else None


def trace_id_of(value: str | None) -> str | None:
    """The 32-hex trace id in a traceparent ("00-<trace id>-<span id>-<flags>")."""
    parts = (value or "").split("-")
    return parts[1] if len(parts) == 4 and len(parts[1]) == 32 else None


def open_incident(incident_id: Any, *, tenant_id: str, service: str, title: str, priority: int) -> str | None:
    """Start (and end) a new incident's root span: a trace of its own, linked to the request
    that opened it. Returns its traceparent, to be stored with the incident."""
    current = trace.get_current_span().get_span_context()
    span = TRACER.start_span(
        f"incident {title} {service}",
        context=otel_context.Context(),  # a new trace, not the webhook request's
        links=[Link(current)] if current.is_valid else None,
        attributes={
            "sre.incident.id": str(incident_id),
            "sre.tenant": tenant_id,
            "sre.incident.service": service,
            "sre.incident.title": title,
            "sre.incident.priority": PRIORITIES[priority],
        },
    )
    span.end()
    return traceparent(span)


@contextmanager
def job_span(job: Job) -> Iterator[Span]:
    """The span of one job attempt, continuing the trace the job was enqueued in."""
    with TRACER.start_as_current_span(
        f"job {job.kind}",
        context=context_of(job.trace_context),
        kind=SpanKind.CONSUMER,
        attributes={
            "sre.job.id": job.id,
            "sre.job.kind": job.kind,
            "sre.job.attempt": job.attempts,
            "sre.job.priority": PRIORITIES[job.priority],
            "sre.job.queue_wait_s": round(job.waited_s, 3),
            "sre.incident.id": str(job.incident_id) if job.incident_id else "",
            "sre.tenant": job.tenant_id,
        },
    ) as span:
        yield span


def event(name: str, **attributes: Any) -> None:
    """A point in time on the current span (a state change, a verification sample)."""
    trace.get_current_span().add_event(name, {k: v for k, v in attributes.items() if v is not None})


def fail(span: Span, error_type: str, message: str = "") -> None:
    span.set_attribute(ERROR_TYPE, error_type)
    span.set_status(Status(StatusCode.ERROR, message[:200] or error_type))


class TraceIdFilter(logging.Filter):
    """Puts the current trace id on every log record (``%(trace_id)s``), so a log line leads
    to its trace and a trace to its log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else "-"
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s trace=%(trace_id)s %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT)
    for handler in logging.getLogger().handlers:
        handler.addFilter(TraceIdFilter())
