---
id: RB-PROMQL
kind: runbook
title: Querying the SLI recording rules and span metrics
services: [all]
version: "2026-10-28"
---
All service metrics come from spans (the span metrics connector), so every service has the
same metric names. Use the recording rules first; they are cheap and already per service.

## Recording rules

- `service:sli_requests:rate5m` and `service:sli_errors:rate5m`: server requests and failed
  server requests per second, label `service_name`. Also `:rate1m` and `:rate30m`.
- `service:sli_error_ratio:rate5m`: errors divided by requests. This is what the alert uses.
- `service:sli_latency_p95_ms:rate5m`: p95 server latency in milliseconds.

`topk(10, service:sli_error_ratio:rate5m)` shows every failing service at once.

## Client spans: how a service sees its dependencies

Server-span metrics say a service is failing; client-span metrics say which call fails.

    sum by (service_name, span_name) (
      rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_CLIENT",
           status_code="STATUS_CODE_ERROR"}[5m]))

For checkout, a span name like `oteldemo.PaymentService/Charge` with errors while all other
client spans are clean points at the payment hop. Whether payment or checkout is at fault
depends on payment's own server spans: if payment's server spans also fail, payment is
broken; if payment has no matching server spans at all, the requests never arrived.

## Latency by hop

    histogram_quantile(0.95, sum by (service_name, span_name, le) (
      rate(traces_span_metrics_duration_milliseconds_bucket{span_kind="SPAN_KIND_CLIENT"}[5m])))

When every client span of one service slows by the same amount, whatever it calls, the
service's own network or CPU is the suspect, not its dependencies.

## Pitfalls

Use `rate` over a window at least four times the scrape interval. A service with no
traffic has no series at all, not a zero; absence is not health.
