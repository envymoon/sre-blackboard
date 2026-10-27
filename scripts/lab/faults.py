"""The fault catalogue: what can be broken in the lab, and how to undo it.

Three mechanisms:

- ``flag``: an OpenTelemetry Demo feature flag (flagd). The fault is in the application code
  of one service, switched on at runtime.
- ``chaos``: a Chaos Mesh object (network delay, pod failure, CPU stress). Deleting the object
  undoes it.
- ``release``: a bad rollout of a Deployment, which leaves rollout history behind. Undone with
  ``kubectl rollout undo``, the same action M5's executor will propose.

``root_cause`` is the service where the fault is injected (the problem host). ``symptoms``
lists the services expected to alert (the event hosts); they often differ, which is exactly
what the diagnosis has to work out. Expectations are refined from lab runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

NAMESPACE = "otel-demo"
COMPONENT_LABEL = "app.kubernetes.io/component"
CHAOS_API = "chaos-mesh.org/v1alpha1"
# Marks objects and rollouts made by this tool, so recovery only undoes its own changes.
CHANGE_CAUSE = "kubernetes.io/change-cause"
MANAGED_BY = {"app.kubernetes.io/managed-by": "sre-lab"}


@dataclass(frozen=True)
class FlagFault:
    flag: str
    variant: str


@dataclass(frozen=True)
class ChaosFault:
    kind: Literal["NetworkChaos", "PodChaos", "StressChaos"]
    spec: dict[str, Any]


@dataclass(frozen=True)
class ReleaseFault:
    deployment: str
    container: str
    env: dict[str, str]
    change_cause: str


@dataclass(frozen=True)
class Fault:
    name: str
    description: str
    root_cause: str
    fault_type: str
    symptoms: tuple[str, ...]
    mechanism: FlagFault | ChaosFault | ReleaseFault
    tags: tuple[str, ...] = field(default=())
    # M5: the repair from the action catalogue a human would approve, e.g. ("rollout_undo",
    # "checkout"). The acceptance run approves a plan that is exactly this and rejects any other.
    # None: nothing in the catalogue repairs this fault; its plans are left for the lab to close.
    repair: tuple[str, str] | None = None

    @property
    def kind(self) -> str:
        return {FlagFault: "flag", ChaosFault: "chaos", ReleaseFault: "release"}[type(self.mechanism)]

    def chaos_name(self) -> str:
        return f"lab-{self.name}"


def _selector(service: str) -> dict[str, Any]:
    return {"namespaces": [NAMESPACE], "labelSelectors": {COMPONENT_LABEL: service}}


CATALOGUE: tuple[Fault, ...] = (
    # --- feature flags --------------------------------------------------------------------
    Fault(
        name="payment-failure",
        description="payment rejects half of all charges",
        root_cause="payment",
        fault_type="error",
        symptoms=("payment", "checkout"),
        mechanism=FlagFault("paymentFailure", "50%"),
        tags=("acceptance",),
    ),
    # The same flag at other strengths (M7): a trickle of failures and a total outage.
    Fault(
        name="payment-failure-10",
        description="payment rejects one in ten charges",
        root_cause="payment",
        fault_type="error",
        symptoms=("payment", "checkout"),
        mechanism=FlagFault("paymentFailure", "10%"),
    ),
    Fault(
        name="payment-failure-100",
        description="payment rejects every charge",
        root_cause="payment",
        fault_type="error",
        symptoms=("payment", "checkout"),
        mechanism=FlagFault("paymentFailure", "100%"),
    ),
    Fault(
        name="payment-unreachable",
        description="checkout calls a payment address that does not exist",
        # The flag is read by checkout, which then dials a bad address; payment itself is
        # healthy and never sees the calls. The service to fix is checkout (M7 relabel).
        root_cause="checkout",
        fault_type="dependency-unreachable",
        symptoms=("checkout",),
        mechanism=FlagFault("paymentUnreachable", "on"),
    ),
    Fault(
        name="cart-failure",
        description="cart fails half of EmptyCart calls (bad store)",
        root_cause="cart",
        fault_type="error",
        symptoms=("cart", "checkout"),
        mechanism=FlagFault("cartFailure", "50%"),
    ),
    Fault(
        name="product-catalog-failure",
        description="product-catalog fails GetProduct for one product",
        root_cause="product-catalog",
        fault_type="error",
        symptoms=("product-catalog", "frontend"),
        mechanism=FlagFault("productCatalogFailure", "on"),
    ),
    Fault(
        name="product-catalog-lock-contention",
        description="product-catalog queries wait on a database lock",
        root_cause="product-catalog",
        fault_type="latency",
        symptoms=("product-catalog", "frontend"),
        mechanism=FlagFault("productCatalogLockContention", "on"),
    ),
    Fault(
        name="ad-failure",
        description="ad fails one in ten GetAds calls",
        root_cause="ad",
        fault_type="error",
        symptoms=("ad",),
        mechanism=FlagFault("adFailure", "on"),
    ),
    Fault(
        name="ad-high-cpu",
        description="ad burns CPU on every request",
        root_cause="ad",
        fault_type="saturation",
        symptoms=("ad",),
        mechanism=FlagFault("adHighCpu", "on"),
    ),
    Fault(
        name="recommendation-cache-failure",
        description="recommendation's cache leaks memory and slows down",
        root_cause="recommendation",
        fault_type="memory-leak",
        symptoms=("recommendation", "frontend"),
        mechanism=FlagFault("recommendationCacheFailure", "on"),
    ),
    Fault(
        name="email-memory-leak",
        description="email leaks memory on every confirmation until it is OOM-killed",
        root_cause="email",
        fault_type="memory-leak",
        symptoms=("email", "checkout"),
        mechanism=FlagFault("emailMemoryLeak", "100x"),
    ),
    Fault(
        name="intl-shipping-slowdown",
        description="shipping sleeps 5s on every order outside the US",
        root_cause="shipping",
        fault_type="latency",
        symptoms=("shipping", "checkout"),
        mechanism=FlagFault("intlShippingSlowdown", "5sec"),
    ),
    Fault(
        name="cart-readiness-failure",
        description="cart's readiness probe fails, so it is removed from its Service",
        root_cause="cart",
        fault_type="unavailable",
        symptoms=("frontend", "checkout"),
        mechanism=FlagFault("failedReadinessProbe", "on"),
    ),
    # --- Chaos Mesh -----------------------------------------------------------------------
    Fault(
        name="network-delay",
        description="1s of added network latency on product-catalog",
        root_cause="product-catalog",
        fault_type="network-latency",
        # Observed: product-catalog's own spans slow down too (every request queries
        # Postgres, GetProduct also asks flagd, both over the delayed link), and so does
        # everything above it, including checkout (GetProduct for every cart item) once
        # it has enough orders to pass the traffic guard.
        symptoms=("product-catalog", "recommendation", "checkout", "frontend", "frontend-proxy"),
        mechanism=ChaosFault(
            "NetworkChaos",
            {
                "action": "delay",
                "mode": "all",
                "selector": _selector("product-catalog"),
                "delay": {"latency": "1000ms", "jitter": "100ms", "correlation": "0"},
                "direction": "to",
            },
        ),
        tags=("acceptance",),
    ),
    Fault(
        name="pod-failure",
        description="every cart pod stops serving (container replaced by a pause image)",
        root_cause="cart",
        fault_type="unavailable",
        symptoms=("frontend", "checkout"),
        mechanism=ChaosFault("PodChaos", {"action": "pod-failure", "mode": "all", "selector": _selector("cart")}),
    ),
    Fault(
        name="cpu-stress",
        description="CPU stress inside the recommendation pod",
        root_cause="recommendation",
        fault_type="saturation",
        symptoms=("recommendation", "frontend"),
        mechanism=ChaosFault(
            "StressChaos",
            {
                "mode": "all",
                "selector": _selector("recommendation"),
                "stressors": {"cpu": {"workers": 2, "load": 100}},
            },
        ),
    ),
    # --- bad release ----------------------------------------------------------------------
    Fault(
        name="bad-release",
        description="a checkout rollout points its payment client at a port nobody listens on",
        root_cause="checkout",
        fault_type="bad-deploy",
        # Observed: only checkout. Orders are ~3% of frontend traffic, below its burn
        # thresholds, and payment never sees the requests.
        symptoms=("checkout",),
        mechanism=ReleaseFault(
            deployment="checkout",
            container="checkout",
            env={"PAYMENT_ADDR": "payment:9999"},
            change_cause="checkout config release: payment client moved to port 9999",
        ),
        tags=("acceptance",),
        repair=("rollout_undo", "checkout"),
    ),
    # More bad config releases (M7): each points one client at a port nobody listens on. The
    # process starts and passes its probes, so the rollout completes and only requests fail.
    Fault(
        name="frontend-bad-release",
        description="a frontend rollout points its recommendation client at a dead port",
        root_cause="frontend",
        fault_type="bad-deploy",
        # recommendation itself stays healthy: it never sees the requests.
        symptoms=("frontend", "frontend-proxy"),
        mechanism=ReleaseFault(
            deployment="frontend",
            container="frontend",
            env={"RECOMMENDATION_ADDR": "recommendation:9999"},
            change_cause="frontend config release: recommendation client moved to port 9999",
        ),
        repair=("rollout_undo", "frontend"),
    ),
    Fault(
        name="recommendation-bad-release",
        description="a recommendation rollout points its product-catalog client at a dead port",
        root_cause="recommendation",
        fault_type="bad-deploy",
        symptoms=("recommendation", "frontend"),
        mechanism=ReleaseFault(
            deployment="recommendation",
            container="recommendation",
            env={"PRODUCT_CATALOG_ADDR": "product-catalog:9999"},
            change_cause="recommendation config release: catalog client moved to port 9999",
        ),
        repair=("rollout_undo", "recommendation"),
    ),
    Fault(
        name="shipping-bad-release",
        description="a shipping rollout points its quote client at a dead port",
        root_cause="shipping",
        fault_type="bad-deploy",
        symptoms=("shipping", "checkout", "frontend"),
        mechanism=ReleaseFault(
            deployment="shipping",
            container="shipping",
            env={"QUOTE_ADDR": "http://quote:9999"},
            change_cause="shipping config release: quote client moved to port 9999",
        ),
        repair=("rollout_undo", "shipping"),
    ),
    # --- harmless releases (M7) --------------------------------------------------------------
    # Distractors: a real rollout with a plausible change-cause that changes nothing a request
    # touches (an environment variable the service never reads). Injected shortly before an
    # unrelated fault, they leave exactly the trail a bad release would.
    Fault(
        name="benign-release-currency",
        description="a currency rollout that changes nothing requests depend on",
        root_cause="currency",
        fault_type="benign-deploy",
        symptoms=(),
        mechanism=ReleaseFault(
            deployment="currency",
            container="currency",
            env={"CURRENCY_RATES_REFRESH": "15m"},
            change_cause="currency release: refresh exchange rates every 15 minutes",
        ),
        tags=("benign",),
    ),
    Fault(
        name="benign-release-frontend",
        description="a frontend rollout that changes nothing requests depend on",
        root_cause="frontend",
        fault_type="benign-deploy",
        symptoms=(),
        mechanism=ReleaseFault(
            deployment="frontend",
            container="frontend",
            env={"PROMO_BANNER": "autumn-sale"},
            change_cause="frontend release: autumn sale banner",
        ),
        tags=("benign",),
    ),
    Fault(
        name="benign-release-checkout",
        description="a checkout rollout that changes nothing requests depend on",
        root_cause="checkout",
        fault_type="benign-deploy",
        symptoms=(),
        mechanism=ReleaseFault(
            deployment="checkout",
            container="checkout",
            env={"ORDER_CONFIRMATION_TEMPLATE": "v2"},
            change_cause="checkout release: new order confirmation template",
        ),
        tags=("benign",),
    ),
)

BY_NAME = {f.name: f for f in CATALOGUE}


def get(name: str) -> Fault:
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown fault {name!r}; known: {', '.join(BY_NAME)}") from None


def chaos_manifest(fault: Fault) -> dict[str, Any]:
    """The Chaos Mesh object for a chaos fault. It runs until deleted (no duration)."""
    assert isinstance(fault.mechanism, ChaosFault)
    return {
        "apiVersion": CHAOS_API,
        "kind": fault.mechanism.kind,
        "metadata": {"name": fault.chaos_name(), "namespace": NAMESPACE, "labels": dict(MANAGED_BY)},
        "spec": fault.mechanism.spec,
    }


def release_patch(fault: Fault) -> dict[str, Any]:
    """A strategic merge patch that rolls out the bad config and records why.

    The change-cause annotation is copied into the new ReplicaSet, so it shows up in
    ``kubectl rollout history``, where a diagnosis can find it.
    """
    m = fault.mechanism
    assert isinstance(m, ReleaseFault)
    return {
        "metadata": {"annotations": {CHANGE_CAUSE: m.change_cause}},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": m.container, "env": [{"name": k, "value": v} for k, v in sorted(m.env.items())]}
                    ]
                }
            }
        },
    }


def with_flag(flags: dict[str, Any], flag: str, variant: str) -> dict[str, Any]:
    """Return a copy of a flagd ``flags`` object with one flag's default variant changed."""
    if flag not in flags:
        raise KeyError(f"flag {flag!r} does not exist in this demo version")
    variants = flags[flag].get("variants", {})
    if variant not in variants:
        raise KeyError(f"flag {flag!r} has no variant {variant!r}; variants: {', '.join(variants)}")
    updated = {name: dict(spec) for name, spec in flags.items()}
    updated[flag]["defaultVariant"] = variant
    return updated
