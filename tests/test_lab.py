"""The fault lab tooling, without a cluster: catalogue, kubectl calls, flagd writes, acceptance."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine, text

from app.tools.sources import Cassette
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
from scripts.lab.probes import ProbeResult, ProbeRunner
from tests.helpers.fake_backends import NOW, FakeBackends
from tests.helpers.fake_backends import settings as fake_settings

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


PAYMENT = {"alertname": "ErrorBudgetBurn", "service_name": "payment"}
FAST_BURN = {**PAYMENT, "burn": "fast"}
SLOW_BURN = {**PAYMENT, "burn": "slow"}


class SimulatedLab:
    """Stands in for the cluster: injecting makes 'Alertmanager' post the ``on_inject``
    notifications into the database, recovering posts ``on_recover``."""

    def __init__(
        self,
        engine: Engine,
        on_inject: list[tuple[str, dict[str, str]]] | None = None,
        on_recover: list[tuple[str, dict[str, str]]] | None = None,
        prometheus: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.engine = engine
        self.on_inject = [("firing", PAYMENT)] if on_inject is None else on_inject
        self.on_recover = [("resolved", PAYMENT)] if on_recover is None else on_recover
        self.prometheus = prometheus or []  # successive answers of alerts(), then none
        self.injected: list[str] = []
        self.recovered: list[str] = []

    def alerts(self) -> list[dict[str, Any]]:
        return self.prometheus.pop(0) if self.prometheus else []

    def _post(self, status: str, labels: dict[str, str]) -> None:
        # Same incident fingerprint for fast and slow burn, as in app.ingest.identify.
        fingerprint = f"fp-{labels['alertname']}-{labels['service_name']}"
        with self.engine.begin() as conn:
            incident_id = conn.execute(
                text(
                    "INSERT INTO incidents (tenant_id, fingerprint, title, service, environment, priority) "
                    "VALUES ('lab', :fp, :t, :svc, 'lab', 1) "
                    "ON CONFLICT (tenant_id, fingerprint) WHERE closed_at IS NULL DO UPDATE SET title = EXCLUDED.title "
                    "RETURNING id"
                ),
                {"fp": fingerprint, "t": labels["alertname"], "svc": labels["service_name"]},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO alerts (tenant_id, incident_id, fingerprint, status, labels) "
                    "VALUES ('lab', :i, :fp, :s, CAST(:l AS jsonb))"
                ),
                {"i": incident_id, "fp": fingerprint, "s": status, "l": json.dumps(labels)},
            )

    def inject(self, fault: Any) -> None:
        self.injected.append(fault.name)
        for status, labels in self.on_inject:
            self._post(status, labels)

    def recover(self, fault: Any) -> bool:
        self.recovered.append(fault.name)
        for status, labels in self.on_recover:
            self._post(status, labels)
        return True


@pytest.fixture
def sync_engine(migrated_url: str) -> Any:
    eng = create_engine(migrated_url)
    with eng.begin() as conn:
        conn.execute(text("TRUNCATE jobs, alerts, incidents, audit_log RESTART IDENTITY CASCADE"))
    yield eng
    eng.dispose()


FAKE_NOW = datetime.fromtimestamp(NOW, UTC)
FAST = {"settle_s": 0.2, "poll_s": 0.05, "quiet_timeout": 1, "hold_s": 0.1}


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
    sim = SimulatedLab(sync_engine, on_inject=[], on_recover=[])
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
    sim = SimulatedLab(sync_engine, on_recover=[])
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


def _accept_one(sim: SimulatedLab, engine: Engine, name: str = "payment-failure", **kwargs: Any) -> Any:
    opts = {**FAST, **kwargs}
    (result,) = accept.run(sim, engine, [get(name)], tenant="lab", log=lambda _: None, **opts)  # type: ignore[arg-type]
    return result


def test_fast_to_slow_burn_handover_is_not_flapping(sync_engine: Engine) -> None:
    # Both burns share the incident fingerprint. After recovery the fast burn resolves and the
    # slow burn (inhibited until then) shows up as firing, then resolves too.
    sim = SimulatedLab(
        sync_engine,
        on_inject=[("firing", FAST_BURN)],
        on_recover=[("resolved", FAST_BURN), ("firing", SLOW_BURN), ("resolved", SLOW_BURN)],
    )
    result = _accept_one(sim, sync_engine)
    assert result.flapping == []
    assert [a.alertname for a in result.alerts] == ["ErrorBudgetBurn[fast]"]
    assert result.ok(alert_timeout=300), result


def test_alert_resolving_while_the_fault_is_active_is_flapping(sync_engine: Engine) -> None:
    sim = SimulatedLab(sync_engine, on_inject=[("firing", PAYMENT), ("resolved", PAYMENT), ("firing", PAYMENT)])
    result = _accept_one(sim, sync_engine)
    assert result.flapping == ["ErrorBudgetBurn/payment"]
    assert not result.ok(alert_timeout=300)
    assert "| ErrorBudgetBurn/payment |" in accept.to_markdown([result], 300)


def test_alert_only_on_unexpected_services_fails(sync_engine: Engine) -> None:
    ad = {"alertname": "ErrorBudgetBurn", "service_name": "ad"}
    sim = SimulatedLab(sync_engine, on_inject=[("firing", ad)], on_recover=[("resolved", ad)])
    result = _accept_one(sim, sync_engine)
    assert result.resolved_s is not None and not result.symptom_matched
    assert not result.ok(alert_timeout=300)


def test_pending_alerts_keep_the_lab_from_being_quiet() -> None:
    pending = {
        "state": "pending",
        "labels": {"alertname": "ErrorBudgetBurn", "service_name": "cart", "environment": "lab"},
    }
    other_env = {"state": "firing", "labels": {"alertname": "Watchdog", "environment": "prod"}}
    lab = SimulatedLab(None, prometheus=[[pending], [pending], [other_env], []])  # type: ignore[arg-type]
    assert accept.wait_quiet(lab, timeout_s=5, log=lambda _: None, hold_s=0.05, poll_s=0.02)  # type: ignore[arg-type]
    assert lab.prometheus == []  # it waited through both pending answers

    logs: list[str] = []
    stuck = SimulatedLab(None, prometheus=[[pending]] * 100)  # type: ignore[arg-type]
    assert not accept.wait_quiet(stuck, timeout_s=0.1, log=logs.append, hold_s=0.05, poll_s=0.02)  # type: ignore[arg-type]
    assert "ErrorBudgetBurn/cart (pending)" in logs[0]


def test_baseline_reports_any_alert_on_a_healthy_lab() -> None:
    pending = {
        "state": "pending",
        "labels": {"alertname": "ErrorBudgetBurn", "service_name": "frontend", "environment": "lab"},
    }
    firing = {**pending, "state": "firing"}
    noisy = SimulatedLab(None, prometheus=[[], [pending], [firing], [firing], []])  # type: ignore[arg-type]
    logs: list[str] = []
    noise = accept.check_baseline(noisy, duration_s=0.3, log=logs.append, poll_s=0.02)  # type: ignore[arg-type]
    assert noise == ["ErrorBudgetBurn/frontend (firing)", "ErrorBudgetBurn/frontend (pending)"]
    assert len(logs) == 2  # each alert logged once

    quiet = SimulatedLab(None)  # type: ignore[arg-type]
    assert accept.check_baseline(quiet, duration_s=0.05, log=lambda _: None, poll_s=0.02) == []  # type: ignore[arg-type]


def test_annotations_escape_chunk_and_cap(tmp_path: Path) -> None:
    from scripts.lab.annotate import build

    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {i} 100%" for i in range(100)))
    notes = build([f"{log}:3", str(tmp_path / "missing.txt")])
    assert notes == ["::notice title=x.log [1/1]::line 97 100%25%0Aline 98 100%25%0Aline 99 100%25"]
    many = build([str(log)], chunk=50, limit=4)
    assert len(many) == 4 and many[0].startswith("::notice title=x.log [1/")


# --- M3: tool checks during a fault ----------------------------------------------------------


def test_a_failed_tool_check_fails_the_fault(sync_engine: Engine) -> None:
    calls: list[str] = []

    def probe(fault: Any) -> list[ProbeResult]:
        calls.append(fault.name)
        return [
            ProbeResult("error_ratio", "prom_query", {}, True, "payment=0.5"),
            ProbeResult("flags", "flag_state", {}, False, "on: []"),
        ]

    result = _accept_one(SimulatedLab(sync_engine), sync_engine, probe=probe)
    assert calls == ["payment-failure"]
    assert result.resolved_s is not None and result.symptom_matched
    assert not result.ok(alert_timeout=300)
    assert "| 1/2 (failed: flags) |" in accept.to_markdown([result], 300)


def test_probes_check_live_answers_and_record_a_replayable_cassette(tmp_path: Path) -> None:
    fake = FakeBackends(flag_variants={"paymentFailure": "50%"})
    runner = ProbeRunner(fake_settings(), tmp_path, transport=httpx.MockTransport(fake.handler), now=lambda: FAKE_NOW)
    payment = runner(get("payment-failure"))
    assert [(p.name, p.ok) for p in payment] == [
        ("error_ratio", True),
        ("error_trend", True),
        ("flags", True),
        ("error_traces", True),
        ("logs", True),
    ], [p.detail for p in payment]
    assert all(p.evidence_id and p.evidence_id.startswith("ev_") for p in payment)

    # The fake's error spans are in payment, not checkout: that check must notice.
    release = {p.name: p for p in runner(get("bad-release"))}
    assert not release["error_traces"].ok
    assert all(p.ok for name, p in release.items() if name != "error_traces"), [p.detail for p in release.values()]

    cassette = Cassette.load(tmp_path / "payment-failure.jsonl.gz")
    assert cassette.meta["root_cause"] == "payment"
    assert [e["tool"] for e in cassette.entries] == [
        "prom_query",
        "prom_range",
        "flag_state",
        "traces_search",
        "logs_search",
    ]


class DiagnosingLab(SimulatedLab):
    """As SimulatedLab, and 'the agents' diagnose every incident the fault opens right away."""

    def __init__(self, engine: Engine, top: str) -> None:
        super().__init__(engine)
        self.top = top

    def inject(self, fault: Any) -> None:
        super().inject(fault)
        body = {
            "summary": "s",
            "root_causes": [
                {"service": self.top, "cause": "c", "confidence": 0.8, "evidence_ids": ["ev_0000000000000001"]}
            ],
            "facts": [],
            "inferences": [],
            "hypotheses": [],
        }
        with self.engine.begin() as conn:
            for row in conn.execute(text("SELECT id, tenant_id FROM incidents WHERE state = 'RECEIVED'")).all():
                conn.execute(text("UPDATE incidents SET state = 'DIAGNOSED' WHERE id = :id"), {"id": row.id})
                conn.execute(
                    text(
                        "INSERT INTO blackboard (tenant_id, incident_id, kind, author, body) "
                        "VALUES (:t, :id, 'diagnosis', 'synthesizer', CAST(:b AS jsonb))"
                    ),
                    {"t": row.tenant_id, "id": row.id, "b": json.dumps(body)},
                )


@pytest.mark.parametrize(("top", "correct"), [("payment", True), ("checkout", False)])
def test_acceptance_scores_the_diagnosis_and_closes_the_incidents(
    sync_engine: Engine, migrated_url: str, tmp_path: Path, top: str, correct: bool
) -> None:
    from scripts.lab.diagnose import Diagnoser

    with sync_engine.begin() as conn:
        conn.execute(text("TRUNCATE blackboard, agent_runs, llm_calls, tool_calls, evidence CASCADE"))
    diagnoser = Diagnoser(sync_engine, migrated_url, "lab", tmp_path, timeout_s=5, planning=False)
    (result,) = accept.run(
        DiagnosingLab(sync_engine, top),  # type: ignore[arg-type]
        sync_engine,
        [get("payment-failure")],
        tenant="lab",
        log=lambda _: None,
        diagnoser=diagnoser,
        **FAST,
    )
    assert result.ok(alert_timeout=300), result  # a candidate was given, right or wrong
    assert result.answer is not None and result.answer.top == top and result.diagnosis_correct is correct
    markdown = accept.to_markdown([result], 300)
    assert f"Top-1 correct: {int(correct)}/1 (needed: 1)" in markdown
    assert (tmp_path / "payment-failure" / "incidents.json").exists()
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT state, closed_at IS NOT NULL FROM incidents")).all() == [("ESCALATED", True)]


def test_a_closed_loop_needs_the_approval_the_resolution_and_every_step_in_the_audit_trail() -> None:
    from scripts.lab.remediate import LOOP_EVENTS, RemediationSeen

    events = ["incident.created", *LOOP_EVENTS]
    done = RemediationSeen(
        "i", "checkout", ["rollout_undo checkout"], decision="approve", state="RESOLVED", events=events
    )
    assert done.closed_loop
    assert not RemediationSeen("i", "checkout", decision="approve", state="ESCALATED", events=events).closed_loop
    assert not RemediationSeen("i", "checkout", decision="approve", state="RESOLVED", events=events[:-1]).closed_loop
    shuffled = [*events[:2], events[3], events[2], *events[4:]]
    assert not RemediationSeen("i", "checkout", decision="approve", state="RESOLVED", events=shuffled).closed_loop


def test_the_report_shows_each_plan_and_whether_the_loop_closed() -> None:
    from scripts.lab.remediate import LOOP_EVENTS, RemediationSeen

    result = accept.ScenarioResult("bad-release", "checkout", ["checkout"], repair="rollout_undo checkout")
    result.remediated = True
    result.remediations = [
        RemediationSeen(
            "i",
            "checkout",
            ["rollout_undo checkout"],
            verdict="require_approval",
            risk="R3",
            decision="approve",
            state="RESOLVED",
            reason="checkout error ratio over 1m is 0 ratio, below 0.06, for 3 checks in a row",
            approved_to_final_s=170.0,
            executions=["succeeded: rollout_undo checkout (revision 2 -> 3)"],
            events=list(LOOP_EVENTS),
        )
    ]
    markdown = accept.to_markdown([result], 300)
    assert "| bad-release | checkout | rollout_undo checkout | require_approval (R3) | approve | resolved: " in markdown
    assert "| 170s | succeeded: rollout_undo checkout (revision 2 -> 3) |" in markdown
    assert "Closed loop for bad-release (rollout_undo checkout): closed" in markdown
