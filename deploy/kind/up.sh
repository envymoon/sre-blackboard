#!/usr/bin/env bash
# Create (or update) the fault lab: a kind cluster running the OpenTelemetry Demo,
# Loki, Chaos Mesh, SLO alert rules, and Alertmanager wired to the sre-blackboard API.
#
# Idempotent: re-running it applies changes to rules, values and wiring.
# Needs docker, kind, kubectl, helm and uv. The API should be running on the host
# (make up), reachable on port ${SRE_API_PORT:-8000}.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
# shellcheck source=versions.env
source "$here/versions.env"

CLUSTER=sre-lab
NS=otel-demo
API_PORT="${SRE_API_PORT:-8000}"
LAB_TENANT="${SRE_LAB_TENANT:-lab}"
kctl=(kubectl --context "kind-$CLUSTER")

log() { printf '\n==> %s\n' "$*"; }

for tool in docker kind kubectl helm uv; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "creating kind cluster $CLUSTER"
  kind create cluster --config "$here/cluster.yaml" --image "$KIND_NODE_IMAGE" --wait 120s
fi
for ns in "$NS" chaos-mesh; do
  "${kctl[@]}" create namespace "$ns" --dry-run=client -o yaml | "${kctl[@]}" apply -f - >/dev/null
done

log "SLO rules -> ConfigMap sre-slo-rules"
"${kctl[@]}" -n "$NS" create configmap sre-slo-rules --from-file="$here/rules" \
  --dry-run=client -o yaml | "${kctl[@]}" apply -f -
# Prometheus has no config reloader here; a changed hash rolls the pod so new rules load.
rules_hash="$(cat "$here"/rules/*.yml | sha256sum | cut -c1-16)"

mint() {
  (cd "$repo" && uv run --quiet python -m app.core.security mint \
    --sub alertmanager --tenant "$1" --roles ingest --ttl $((90 * 24 * 3600)))
}
log "ingest token for Alertmanager (tenant $LAB_TENANT)"
secret=(--from-literal=token="$(mint "$LAB_TENANT")")
# The evaluation (M7): every alert also goes to one tenant per agent variant, e.g.
# SRE_LAB_VARIANTS="single canary" adds tenants lab-single and lab-canary. The workers map
# them to their variants with SRE_EVAL_TENANT_VARIANTS.
values_extra=()
if [ -n "${SRE_LAB_VARIANTS:-}" ]; then
  overlay="$(mktemp)"
  trap 'rm -f "$overlay"' EXIT
  webhook() {
    printf '            - url: http://sre-blackboard:8000/api/v1/alerts\n'
    printf '              send_resolved: true\n              max_alerts: 0\n'
    printf '              http_config: {authorization: {type: Bearer, credentials_file: /etc/sre-blackboard/%s}}\n' "$1"
  }
  {
    printf 'prometheus:\n  alertmanager:\n    config:\n      receivers:\n'
    printf '        - name: sre-blackboard\n          webhook_configs:\n'
    webhook token
    for v in $SRE_LAB_VARIANTS; do webhook "token-$v"; done
  } > "$overlay"
  for v in $SRE_LAB_VARIANTS; do
    log "ingest token for tenant $LAB_TENANT-$v (agent variant $v)"
    secret+=(--from-literal="token-$v=$(mint "$LAB_TENANT-$v")")
  done
  values_extra=(-f "$overlay")
fi
"${kctl[@]}" -n "$NS" create secret generic sre-blackboard-token "${secret[@]}" \
  --dry-run=client -o yaml | "${kctl[@]}" apply -f -

log "Service sre-blackboard -> API on the host"
# Docker Desktop resolves host.docker.internal inside the node; on Linux the kind
# network's gateway is the host.
host_ip="$(docker exec "$CLUSTER-control-plane" getent ahostsv4 host.docker.internal 2>/dev/null | awk 'NR==1 {print $1}' || true)"
if [ -z "$host_ip" ]; then
  host_ip="$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}} {{end}}' | tr ' ' '\n' | grep -E '^[0-9.]+$' | head -1)"
fi
echo "host reachable from the cluster at $host_ip:$API_PORT"
"${kctl[@]}" -n "$NS" apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: sre-blackboard
spec:
  ports:
    - {name: http, port: 8000, targetPort: $API_PORT, protocol: TCP}
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: sre-blackboard-host
  labels:
    kubernetes.io/service-name: sre-blackboard
addressType: IPv4
ports:
  - {name: http, port: $API_PORT, protocol: TCP}
endpoints:
  - addresses: ["$host_ip"]
EOF

log "read-only ServiceAccount for the tools (make lab-env mints its token)"
"${kctl[@]}" -n "$NS" apply -f "$here/manifests/reader.yaml"
log "the executor's ServiceAccount: patch Deployments in $NS, nothing else (M5)"
"${kctl[@]}" -n "$NS" apply -f "$here/manifests/executor.yaml"

log "OTLP into the demo's Jaeger for sre-blackboard's own traces (M8)"
"${kctl[@]}" -n "$NS" apply -f "$here/manifests/jaeger-otlp.yaml"

log "Loki"
sed "s#image: LOKI_IMAGE.*#image: $LOKI_IMAGE#" "$here/manifests/loki.yaml" | "${kctl[@]}" -n "$NS" apply -f -

log "helm repositories"
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts --force-update >/dev/null
helm repo add chaos-mesh https://charts.chaos-mesh.org --force-update >/dev/null
helm repo update open-telemetry chaos-mesh >/dev/null

log "Chaos Mesh $CHAOS_MESH_CHART_VERSION"
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh --kube-context "kind-$CLUSTER" -n chaos-mesh \
  --version "$CHAOS_MESH_CHART_VERSION" -f "$here/values/chaos-mesh.yaml" --wait --timeout 10m

log "OpenTelemetry Demo chart $OTEL_DEMO_CHART_VERSION (first install pulls about 3 GB of images)"
helm upgrade --install otel-demo open-telemetry/opentelemetry-demo --kube-context "kind-$CLUSTER" -n "$NS" \
  --version "$OTEL_DEMO_CHART_VERSION" -f "$here/values/otel-demo.yaml" "${values_extra[@]}" \
  --set-string "prometheus.server.podAnnotations.sre-blackboard/rules-hash=$rules_hash" \
  --wait --timeout 20m
"${kctl[@]}" -n "$NS" rollout status deployment/loki --timeout 5m

cat <<EOF

Lab is up.
  Demo shop        http://localhost:18080
  Jaeger           http://localhost:18080/jaeger/ui
  Feature flags    http://localhost:18080/feature
  Prometheus       http://localhost:19090
  Alertmanager     http://localhost:19093
  Loki             http://localhost:13100
Alerts are posted to the API at $host_ip:$API_PORT (tenant "$LAB_TENANT"${SRE_LAB_VARIANTS:+, and one tenant per variant: $SRE_LAB_VARIANTS}).
Inject a fault:    make fault SCENARIO=payment-failure   (list: make faults)
EOF
