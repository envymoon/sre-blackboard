"""Fake observability backends for tool tests: one httpx MockTransport answering like Prometheus,
Loki, Jaeger, the Kubernetes API and flagd-ui, with data shaped like the real responses."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import Settings
from app.tools.clients import Clients

NOW = 1_760_500_000  # seconds; the fake data sits just before this


def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        prometheus_url="http://prom.test",
        loki_url="http://loki.test",
        jaeger_url="http://jaeger.test/jaeger/ui",
        flagd_ui_url="http://flagd.test/feature",
        k8s_api_url="https://k8s.test",
        k8s_token="reader-token",
        k8s_namespace="otel-demo",
    )


def prom_vector(series: list[tuple[dict[str, str], str]]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": m, "value": [NOW, v]} for m, v in series]},
    }


def prom_matrix(series: list[tuple[dict[str, str], list[str]]], step: int = 15) -> dict[str, Any]:
    result = [{"metric": m, "values": [[NOW - (len(vs) - i) * step, v] for i, v in enumerate(vs)]} for m, vs in series]
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def loki_streams() -> dict[str, Any]:
    ns = NOW * 10**9
    failed = {
        "structuredMetadata": {
            "detected_level": "error",
            "exception_message": "14 UNAVAILABLE: No connection established. Last error: connect ECONNREFUSED 10.96.174.148:8080",
            "url_path": "/api/products/HQTGWGPNH4",
            "http_response_status_code": "500",
            "trace_id": "abc",
        }
    }
    ok = {"structuredMetadata": {"detected_level": "info", "url_path": "/api/cart"}}
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": "frontend"},
                    "values": [
                        [str(ns - 1_000_000_000), "API request failed", failed],
                        [str(ns - 2_000_000_000), "API request failed", failed],
                        [str(ns - 3_000_000_000), "API request completed", ok],
                    ],
                }
            ],
        },
    }


def _span(span_id: str, op: str, pid: str, start: int, dur: int, parent: str | None = None, **tags: Any) -> dict:
    return {
        "traceID": "t1",
        "spanID": span_id,
        "operationName": op,
        "references": [{"refType": "CHILD_OF", "traceID": "t1", "spanID": parent}] if parent else [],
        "startTime": start,
        "duration": dur,
        "tags": [{"key": k.replace("__", "."), "type": "string", "value": v} for k, v in tags.items()],
        "logs": [],
        "processID": pid,
    }


def jaeger_trace(trace_id: str = "4bf92f3577b34da6a3ce929d0e0e4736", error: bool = True) -> dict[str, Any]:
    t0 = NOW * 10**6
    err = (
        {
            "error": True,
            "otel__status_description": "failed to charge card: invalid token",
            "rpc__grpc__status_code": 13,
        }
        if error
        else {}
    )
    spans = [
        _span("a", "POST /api/checkout", "p1", t0, 900_000),
        _span("b", "oteldemo.CheckoutService/PlaceOrder", "p2", t0 + 10_000, 850_000, parent="a"),
        _span("c", "oteldemo.PaymentService/Charge", "p3", t0 + 700_000, 100_000, parent="b", **err),
    ]
    for s in spans:
        s["traceID"] = trace_id
    return {
        "traceID": trace_id,
        "spans": spans,
        "processes": {
            "p1": {"serviceName": "frontend"},
            "p2": {"serviceName": "checkout"},
            "p3": {"serviceName": "payment"},
        },
    }


def k8s_event(name: str, reason: str, type_: str = "Normal", kind: str = "Pod", api: str = "v1", **kw: Any) -> dict:
    return {
        "metadata": {"name": f"{name}.1", "managedFields": [{"manager": "kubelet"}]},
        "involvedObject": {"kind": kind, "name": name, "apiVersion": api},
        "reason": reason,
        "type": type_,
        "message": kw.get("message", f"{reason} {name}"),
        "count": kw.get("count", 1),
        "lastTimestamp": kw.get("at", "2025-10-15T03:33:00Z"),
    }


def k8s_events() -> dict[str, Any]:
    return {
        "items": [
            k8s_event("checkout-6f48f6dcc5-jzhth", "Started"),
            k8s_event(
                "checkout-6f48f6dcc5-jzhth", "BackOff", "Warning", message="Back-off restarting failed container"
            ),
            k8s_event("checkout", "ScalingReplicaSet", kind="Deployment", api="apps/v1"),
            k8s_event(
                "lab-network-delay", "Applied", kind="NetworkChaos", api="chaos-mesh.org/v1alpha1", message="chaos!"
            ),
            k8s_event("cart-1", "Pulled", at="2025-10-14T01:00:00Z"),  # older than the window
        ]
    }


def deployment() -> dict[str, Any]:
    return {
        "metadata": {
            "name": "checkout",
            "uid": "dep-uid",
            "generation": 3,
            "annotations": {"kubernetes.io/change-cause": "checkout config release: payment client moved to port 9999"},
            "managedFields": [{"manager": "kubectl"}],
        },
        "spec": {"selector": {"matchLabels": {"app.kubernetes.io/name": "checkout"}}},
        "status": {
            "replicas": 1,
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "conditions": [{"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"}],
        },
    }


def replicaset(revision: int, payment_addr: str, cause: str | None, owner: str = "dep-uid") -> dict[str, Any]:
    annotations = {"deployment.kubernetes.io/revision": str(revision)}
    if cause:
        annotations["kubernetes.io/change-cause"] = cause
    return {
        "metadata": {
            "name": f"checkout-rs{revision}",
            "creationTimestamp": f"2025-10-15T03:{10 + revision}:00Z",
            "annotations": annotations,
            "ownerReferences": [{"kind": "Deployment", "uid": owner}],
        },
        "spec": {
            "template": {
                "metadata": {"annotations": {}},
                "spec": {
                    "initContainers": [{"name": "wait-for-kafka", "image": "busybox:latest"}],
                    "containers": [
                        {
                            "name": "checkout",
                            "image": "ghcr.io/open-telemetry/demo:3.1.0-checkout",
                            "env": [
                                {"name": "PAYMENT_ADDR", "value": payment_addr},
                                {"name": "OTEL_SERVICE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "x"}}},
                            ],
                        }
                    ],
                },
            }
        },
        "status": {"replicas": 1 if revision == 2 else 0},
    }


def replicasets() -> dict[str, Any]:
    return {
        "items": [
            replicaset(1, "payment:8080", None),
            replicaset(2, "payment:9999", "checkout config release: payment client moved to port 9999"),
            replicaset(9, "x", None, owner="someone-else"),
        ]
    }


def flags(**on: str) -> dict[str, Any]:
    base = {
        "paymentFailure": {"state": "ENABLED", "defaultVariant": "off", "variants": {"off": 0, "50%": 0.5}},
        "adFailure": {"state": "ENABLED", "defaultVariant": "off", "variants": {"off": False, "on": True}},
    }
    for name, variant in on.items():
        base[name]["defaultVariant"] = variant
    return {"flags": base}


Responder = Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeBackends:
    """Routes by host and path. ``overrides[(host, path)]`` replaces the default answer;
    every request is kept in ``requests``."""

    overrides: dict[tuple[str, str], Responder] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    flag_variants: dict[str, str] = field(default_factory=dict)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.url.host, request.url.path)
        if key in self.overrides:
            return self.overrides[key](request)
        host, path = key
        q = request.url.params
        if host == "prom.test" and path == "/api/v1/query":
            return _json(
                prom_vector(
                    [
                        ({"service_name": "checkout"}, "0.42"),
                        ({"service_name": "payment"}, "0.5"),
                        ({"service_name": "cart"}, "NaN"),
                    ]
                )
            )
        if host == "prom.test" and path == "/api/v1/query_range":
            values = ["0"] * 20 + ["0.5"] * 10 + ["0.1"] * 10
            return _json(prom_matrix([({"service_name": "payment"}, values), ({"service_name": "cart"}, ["0"] * 40)]))
        if host == "loki.test" and path == "/loki/api/v1/query_range":
            return _json(loki_streams())
        if host == "jaeger.test" and path == "/jaeger/ui/api/traces":
            return _json({"data": [jaeger_trace(), jaeger_trace("0af7651916cd43dd8448eb211c80319c", error=False)]})
        if host == "jaeger.test" and path.startswith("/jaeger/ui/api/traces/"):
            return _json({"data": [jaeger_trace(path.rsplit("/", 1)[1])]})
        if host == "k8s.test" and path == "/api/v1/namespaces/otel-demo/events":
            return _json(k8s_events())
        if host == "k8s.test" and path == "/apis/apps/v1/namespaces/otel-demo/deployments/checkout":
            return _json(deployment())
        if host == "k8s.test" and path == "/apis/apps/v1/namespaces/otel-demo/replicasets":
            assert q["labelSelector"] == "app.kubernetes.io/name=checkout"
            return _json(replicasets())
        if host == "flagd.test" and path == "/feature/api/read":
            return _json(flags(**self.flag_variants))
        return httpx.Response(404, json={"message": f"no fake for {host}{path}"})

    def clients(self) -> Clients:
        return Clients.from_settings(settings(), transport=httpx.MockTransport(self.handler))


def _json(body: Any) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(body), headers={"content-type": "application/json"})
