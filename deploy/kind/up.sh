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

log "ingest token for Alertmanager (tenant $LAB_TENANT)"
token="$(cd "$repo" && uv run --quiet python -m app.core.security mint \
  --sub alertmanager --tenant "$LAB_TENANT" --roles ingest --ttl $((90 * 24 * 3600)))"
"${kctl[@]}" -n "$NS" create secret generic sre-blackboard-token --from-literal=token="$token" \
  --dry-run=client -o yaml | "${kctl[@]}" apply -f -

log "Service sre-blackboard -> API on the host"
# Docker Desktop resolves host.docker.internal inside the node; on Linux the kind
# network's gateway is the host.
host_ip="$(docker exec "$CLUSTER-control-plane" getent ahostsv4 host.docker.internal 2>/dev/null | awk 'NR==1 {print $1}')"
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
  --version "$OTEL_DEMO_CHART_VERSION" -f "$here/values/otel-demo.yaml" \
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
Alerts are posted to the API at $host_ip:$API_PORT (tenant "$LAB_TENANT").
Inject a fault:    make fault SCENARIO=payment-failure   (list: make faults)
EOF
