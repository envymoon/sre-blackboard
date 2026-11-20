# Read-only queries by source

Every command goes through `ev.py run --signal <signal> -- <command>`. Replace hosts,
namespaces and label names with the ones the user gave you; label names differ between
setups (`service`, `service_name`, `job`, `app`), so list them first instead of guessing.

## Prometheus (metrics)

HTTP API with `curl -sG` (the `-G` keeps `--data-urlencode` in the query string, which the
allowlist requires):

```
curl -sG http://PROM:9090/api/v1/query --data-urlencode 'query=QUERY'
curl -sG http://PROM:9090/api/v1/query_range --data-urlencode 'query=QUERY' \
  --data-urlencode 'start=2026-10-15T03:00:00Z' --data-urlencode 'end=2026-10-15T03:40:00Z' \
  --data-urlencode 'step=60s'
curl -sG http://PROM:9090/api/v1/labels
curl -sG http://PROM:9090/api/v1/alerts
```

Or `promtool query instant http://PROM:9090 'QUERY'`.

Useful queries (adapt metric and label names):

| Question | PromQL |
|---|---|
| error ratio, every service | `sum by (service) (rate(http_requests_total{code=~"5.."}[5m])) / sum by (service) (rate(http_requests_total[5m]))` |
| p95 latency, every service | `histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket[5m])))` |
| when it started | the error ratio as a `query_range` over the hour before the alert |
| restarts | `increase(kube_pod_container_status_restarts_total[30m]) > 0` |
| OOM kills | `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` |
| saturation | `sum by (pod) (rate(container_cpu_cfs_throttled_periods_total[5m])) / sum by (pod) (rate(container_cpu_cfs_periods_total[5m]))` |

If the setup has recording rules for SLIs, prefer them: they are what the alert used.

## Kubernetes (changes)

```
kubectl -n NS rollout history deploy/NAME
kubectl -n NS rollout history deploy/NAME --revision=2
kubectl -n NS get deploy NAME -o yaml
kubectl -n NS get rs -l app=NAME -o wide
kubectl -n NS get events --sort-by=.lastTimestamp
kubectl -n NS events --for deploy/NAME
kubectl -n NS describe pod POD
kubectl -n NS logs POD -c CONTAINER --since=15m --tail=200
kubectl -n NS logs POD --previous --tail=200
kubectl -n NS top pod
```

To see what a rollout changed, record the Deployment (`get deploy -o yaml`) and its
ReplicaSets (`get rs -o yaml`): each ReplicaSet keeps its revision's pod template.

## Loki (logs)

```
logcli --addr=http://LOKI:3100 query --since=30m --limit=200 '{service_name="NAME"} |= "error"'
curl -sG http://LOKI:3100/loki/api/v1/query_range --data-urlencode 'query={service_name="NAME"} |= "error"' \
  --data-urlencode 'limit=200' --data-urlencode 'since=30m'
curl -sG http://LOKI:3100/loki/api/v1/labels
```

Group before you read: `sum by (service_name) (count_over_time({namespace="NS"} |= "error" [10m]))`
as a metric query shows which services log errors at all.

## Jaeger and Tempo (traces)

```
curl -sG http://JAEGER:16686/api/services
curl -sG http://JAEGER:16686/api/traces --data-urlencode 'service=NAME' \
  --data-urlencode 'tags={"error":"true"}' --data-urlencode 'lookback=15m' --data-urlencode 'limit=20'
curl -sG http://JAEGER:16686/api/traces/TRACE_ID
curl -sG http://TEMPO:3200/api/search --data-urlencode 'q={ status = error && resource.service.name = "NAME" }'
curl -sG http://TEMPO:3200/api/traces/TRACE_ID
```

Trace results are large: record them, then `ev.py show E7 --grep status_description` or
`--grep error` to find the failing spans. The deepest failing span is usually the cause.

## Deploys and config (changes)

```
helm -n NS history RELEASE
helm -n NS status RELEASE
git -C CONFIG_REPO log --since=6.hours --stat
git -C CONFIG_REPO show COMMIT
```
