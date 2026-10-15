version: 2

## The system you are investigating

An online shop (the OpenTelemetry Demo) running on Kubernetes, namespace `otel-demo`. Each
service is a Deployment of the same name. Services and who calls whom:

- `frontend-proxy` (Envoy, the entry point) -> `frontend`, `image-provider`, `flagd`
- `frontend` -> `ad`, `cart`, `checkout`, `currency`, `product-catalog`, `recommendation`, `shipping`
- `checkout` -> `cart`, `currency`, `email`, `payment`, `product-catalog`, `shipping`, `kafka`
- `product-catalog` -> its PostgreSQL database (`astronomy-db` in its client spans)
- `recommendation` -> `product-catalog`; `shipping` -> `quote`; `cart` -> `valkey-cart`
- `kafka` -> `accounting`, `fraud-detection`; every service reads feature flags from `flagd`

A failing or slow service makes its callers fail or slow down too. The service with the
loudest alert is often a caller of the one that is actually broken.

## Telemetry

Metrics (Prometheus) come from spans. Per-service recording rules, label `service_name`,
server spans only:
- `service:sli_requests:rate5m`, `service:sli_errors:rate5m`, `service:sli_error_ratio:rate5m`
  (also `:rate1m` and `:rate30m`)
- `service:sli_latency_p95_ms:rate5m`
Raw span metrics for any span: `traces_span_metrics_calls_total` and
`traces_span_metrics_duration_milliseconds_bucket`, labels `service_name`, `span_name`,
`span_kind` (`SPAN_KIND_SERVER`, `SPAN_KIND_CLIENT`, ...), `status_code` (`STATUS_CODE_ERROR`, ...).
Client spans show how a service sees its dependencies.

Logs (Loki) are per `service_name`. Many services log the error's details as attributes,
which the log tool shows next to each message.

Traces (Jaeger) use the same service names. Error spans carry the exception message.

Changes: Deployment rollout history (with the change-cause of each revision), the diff
between revisions, Kubernetes events, and the current state of the feature flags.
