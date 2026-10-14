"""The fault lab tooling, without a cluster: catalogue, kubectl calls, flagd writes, acceptance."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine, text

from scripts.lab import accept
from scripts.lab.cluster import Kubectl, Lab
from scripts.lab.faults import (
    CATALOGUE,
    CHANGE_CAUSE,
    NAMESPACE,
    ChaosFault,
    FlagFault,
    ReleaseFault,
    chaos_manifest,
    get,
    release_patch,
    with_flag,
)

# The flag file shipped with the pinned demo chart (deploy/kind/versions.env).
DEMO_FLAGS = json.loads(Path("tests/fixtures/otel-demo-0.42.4-flagd.json").read_text())["flags"]


def test_catalogue_names_are_unique_and_kinds_are_known() -> None:
    names = [f.name for f in CATALOGUE]
    assert len(names) == len(set(names))
    assert {f.kind for f in CATALOGUE} == {"flag", "chaos", "release"}
    assert {f.name for f in CATALOGUE if "acceptance" in f.tags} == {"payment-failure", "network-delay", "bad-release"}


@pytest.mark.parametrize("fault", [f for f in CATALOGUE if isinstance(f.mechanism, FlagFault)], ids=lambda f: f.name)
def test_flag_faults_exist_in_the_pinned_demo(fault: Any) -> None:
    flag = fault.mechanism.flag
    assert flag in DEMO_FLAGS
    assert fault.mechanism.variant in DEMO_FLAGS[flag]["variants"]
    assert "off" in DEMO_FLAGS[flag]["variants"], "recovery sets the flag back to off"


def test_with_flag_changes_one_flag_and_validates() -> None:
    updated = with_flag(DEMO_FLAGS, "paymentFailure", "50%")
    assert updated["paymentFailure"]["defaultVariant"] == "50%"
    assert DEMO_FLAGS["paymentFailure"]["defaultVariant"] == "off", "input is not mutated"
    assert updated["adFailure"] == DEMO_FLAGS["adFailure"]
    with pytest.raises(KeyError, match="no variant"):
        with_flag(DEMO_FLAGS, "paymentFailure", "60%")
    with pytest.raises(KeyError, match="does not exist"):
        with_flag(DEMO_FLAGS, "noSuchFlag", "on")


def test_chaos_manifests_target_one_service_in_the_demo_namespace() -> None:
    for fault in (f for f in CATALOGUE if isinstance(f.mechanism, ChaosFault)):
        m = chaos_manifest(fault)
        assert m["apiVersion"] == "chaos-mesh.org/v1alpha1"
        assert m["metadata"] == {
            "name": f"lab-{fault.name}",
            "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/managed-by": "sre-lab"},
        }
        selector = m["spec"]["selector"]
        assert selector["namespaces"] == [NAMESPACE]
        assert selector["labelSelectors"] == {"app.kubernetes.io/component": fault.root_cause}
        assert "duration" not in m["spec"], "runs until recovered"


def test_release_patch_sets_env_and_change_cause() -> None:
    fault = get("bad-release")
    assert isinstance(fault.mechanism, ReleaseFault)
    patch = release_patch(fault)
    assert patch["metadata"]["annotations"][CHANGE_CAUSE] == fault.mechanism.change_cause
    (container,) = patch["spec"]["template"]["spec"]["containers"]
    assert container == {"name": "checkout", "env": [{"name": "PAYMENT_ADDR", "value": "payment:9999"}]}


# --- Lab against fakes --------------------------------------------------------------------


class FakeCluster:
    """Records kubectl calls and serves flagd-ui's read/write API."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.stdin: list[str | None] = []
        self.flags = json.loads(json.dumps(DEMO_FLAGS))
        self.objects: set[str] = set()
        self.change_cause = "initial"

    def run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = cmd[5:]  # drop: kubectl --context C -n NS
        self.calls.append(args)
        self.stdin.append(kwargs.get("input"))
        out = ""
        if args[:2] == ["apply", "-f"]:
            m = json.loads(kwargs["input"])
            self.objects.add(f"{m['kind'].lower()}/{m['metadata']['name']}")
        elif args[0] == "delete":
            self.objects.discard(f"{args[1]}/{args[2]}")
        elif args[0] == "get" and args[1] == "deployment":
            out = json.dumps({"metadata": {"annotations": {CHANGE_CAUSE: self.change_cause}}})
        elif args[0] == "get":
            out = f"{args[1]}/{args[2]}\n" if f"{args[1]}/{args[2]}" in self.objects else ""
        elif args[0] == "patch":
            self.change_cause = json.loads(args[-1])["metadata"]["annotations"][CHANGE_CAUSE]
        elif args[0] == "annotate":
            self.change_cause = args[-2].split("=", 1)[1]
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def http(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feature/api/read":
            return httpx.Response(200, json={"flags": self.flags})
        if request.url.path == "/feature/api/write" and request.method == "POST":
            body = json.loads(request.content)
            assert body["data"]["$schema"] == "https://flagd.dev/schema/v0/flags.json"
            self.flags = body["data"]["flags"]
            return httpx.Response(200, json={})
        return httpx.Response(404)


@pytest.fixture
def cluster() -> FakeCluster:
    return FakeCluster()


@pytest.fixture
def lab(cluster: FakeCluster) -> Lab:
    return Lab(
        kubectl=Kubectl(run=cluster.run),
        base_url="http://lab",
        http=httpx.Client(transport=httpx.MockTransport(cluster.http)),
    )


def test_flag_fault_inject_and_recover(lab: Lab, cluster: FakeCluster) -> None:
    fault = get("payment-failure")
    assert not lab.is_active(fault)
    lab.inject(fault)
    assert cluster.flags["paymentFailure"]["defaultVariant"] == "50%"
    assert cluster.calls == [], "flags go through flagd-ui, not kubectl"
    assert [f.name for f in lab.active()] == ["payment-failure"]
    assert lab.recover(fault) is True
    assert cluster.flags["paymentFailure"]["defaultVariant"] == "off"
    assert lab.recover(fault) is False, "recovering twice is a no-op"


def test_chaos_fault_inject_and_recover(lab: Lab, cluster: FakeCluster) -> None:
    fault = get("network-delay")
    lab.inject(fault)
    assert cluster.calls[0] == ["apply", "-f", "-"]
    assert json.loads(cluster.stdin[0] or "")["kind"] == "NetworkChaos"
    assert lab.is_active(fault)
    assert lab.recover(fault) is True
    assert ["delete", "networkchaos", "lab-network-delay", "--ignore-not-found", "--wait=true"] in cluster.calls
    assert not lab.is_active(fault)


def test_release_fault_rolls_out_and_back(lab: Lab, cluster: FakeCluster) -> None:
    fault = get("bad-release")
    lab.inject(fault)
    assert cluster.calls[0][:4] == ["patch", "deployment", "checkout", "--type=strategic"]
    assert cluster.calls[1] == ["rollout", "status", "deployment/checkout", "--timeout=180s"]
    assert lab.is_active(fault)

    cluster.calls.clear()
    assert lab.recover(fault) is True
    verbs = [c[:2] for c in cluster.calls]
    assert ["rollout", "undo"] in verbs
    assert verbs.index(["rollout", "undo"]) < verbs.index(["annotate", "deployment"])
    assert not lab.is_active(fault)
    cluster.calls.clear()
    assert lab.recover(fault) is False
    assert ["rollout", "undo"] not in [c[:2] for c in cluster.calls], "never undoes a rollout it did not make"


def test_kubectl_failure_raises_with_stderr() -> None:
    def failing(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", 'deployments.apps "x" not found')

    with pytest.raises(RuntimeError, match="not found"):
        Kubectl(run=failing)("get", "deployment", "x")


# --- acceptance flow against a real database ----------------------------------------------


class SimulatedLab:
    """Stands in for the cluster: injecting makes 'Alertmanager' post firing alerts into the
    database, recovering posts the resolutions."""

    def __init__(self, engine: Engine, *, fire: bool = True, resolve: bool = True) -> None:
        self.engine, self.fire, self.resolve = engine, fire, resolve
        self.injected: list[str] = []
        self.recovered: list[str] = []

    def firing_alerts(self) -> list[dict[str, Any]]:
        return []

    def _post(self, status: str, service: str) -> None:
        with self.engine.begin() as conn:
            incident_id = None
            if status == "firing":
                incident_id = conn.execute(
                    text(
                        "INSERT INTO incidents (tenant_id, fingerprint, title, service, environment, priority) "
                        "VALUES ('lab', :fp, 'ErrorBudgetBurn', :svc, 'lab', 1) RETURNING id"
                    ),
                    {"fp": uuid.uuid4().hex, "svc": service},
                ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO alerts (tenant_id, incident_id, fingerprint, status, labels) "
                    "VALUES ('lab', :i, :fp, :s, CAST(:l AS jsonb))"
                ),
                {
                    "i": incident_id,
                    "fp": f"fp-{service}",
                    "s": status,
                    "l": json.dumps({"alertname": "ErrorBudgetBurn", "service_name": service}),
                },
            )

    def inject(self, fault: Any) -> None:
        self.injected.append(fault.name)
        if self.fire:
            self._post("firing", "payment")

    def recover(self, fault: Any) -> bool:
        self.recovered.append(fault.name)
        if self.resolve:
            self._post("resolved", "payment")
        return True


@pytest.fixture
def sync_engine(migrated_url: str) -> Any:
    eng = create_engine(migrated_url)
    with eng.begin() as conn:
        conn.execute(text("TRUNCATE jobs, alerts, incidents, audit_log RESTART IDENTITY CASCADE"))
    yield eng
    eng.dispose()


FAST = {"settle_s": 0.2, "poll_s": 0.05, "quiet_timeout": 1}


def test_acceptance_passes_when_alert_arrives_and_resolves(sync_engine: Engine) -> None:
    sim = SimulatedLab(sync_engine)
    (result,) = accept.run(sim, sync_engine, [get("payment-failure")], tenant="lab", log=lambda _: None, **FAST)  # type: ignore[arg-type]
    assert result.ok(alert_timeout=300), result
    assert result.first_alert_s is not None and result.first_alert_s < 5
    assert [(a.alertname, a.service) for a in result.alerts] == [("ErrorBudgetBurn", "payment")]
    assert result.symptom_matched and result.incident_created
    assert sim.injected == sim.recovered == ["payment-failure"]
    report = json.loads(accept.to_json([result], 300))
    assert report[0]["ok"] is True
    assert "| payment-failure | payment |" in accept.to_markdown([result], 300)


def test_acceptance_fails_without_alert_and_still_recovers(sync_engine: Engine) -> None:
    sim = SimulatedLab(sync_engine, fire=False)
    (result,) = accept.run(
        sim,  # type: ignore[arg-type]
        sync_engine,
        [get("bad-release")],
        tenant="lab",
        log=lambda _: None,
        alert_timeout=0.3,
        resolve_timeout=0.3,
        **FAST,
    )
    assert not result.ok(alert_timeout=0.3)
    assert result.first_alert_s is None
    assert sim.recovered == ["bad-release"]


def test_acceptance_reports_unresolved_alerts(sync_engine: Engine) -> None:
    sim = SimulatedLab(sync_engine, resolve=False)
    (result,) = accept.run(
        sim,
        sync_engine,
        [get("payment-failure")],
        tenant="lab",
        log=lambda _: None,
        resolve_timeout=0.3,
        **FAST,  # type: ignore[arg-type]
    )
    assert result.resolved_s is None
    assert result.unresolved == ["ErrorBudgetBurn/payment"]
    assert not result.ok(alert_timeout=300)


def test_annotations_escape_chunk_and_cap(tmp_path: Path) -> None:
    from scripts.lab.annotate import build

    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {i} 100%" for i in range(100)))
    notes = build([f"{log}:3", str(tmp_path / "missing.txt")])
    assert notes == ["::notice title=x.log [1/1]::line 97 100%25%0Aline 98 100%25%0Aline 99 100%25"]
    many = build([str(log)], chunk=50, limit=4)
    assert len(many) == 4 and many[0].startswith("::notice title=x.log [1/")
