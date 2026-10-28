"""The evaluation's lab runner (scripts/lab/evaluate.py) on a simulated lab and a real database:
distractors before the fault, every variant's tenant, observations per tenant."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import incidents
from app.eval import scenarios
from app.eval.results import ResultFile, score_all
from app.eval.score import MODEL_UNAVAILABLE, IncidentObs
from scripts.lab import evaluate
from scripts.lab.diagnose import Diagnoser

TENANTS = {"lab": "payment", "lab-single": "checkout"}  # tenant -> what its "agents" answer
FAST = {"settle_s": 0.2, "poll_s": 0.05, "quiet_timeout": 1, "hold_s": 0.1}


class TwoTenantLab:
    """Injecting the fault makes 'Alertmanager' post one alert to each tenant, and each tenant's
    'agents' diagnose it at once (through the real state machine, so it is audited)."""

    def __init__(self, engine: Engine, database_url: str) -> None:
        self.engine = engine
        self.database_url = database_url
        self.injected: list[str] = []
        self.recovered: list[str] = []

    def alerts(self) -> list[dict[str, Any]]:
        return []

    def _post(self, tenant: str, status: str) -> None:
        labels = {"alertname": "ErrorBudgetBurn", "service_name": "checkout", "slo": "availability"}
        with self.engine.begin() as conn:
            incident_id = conn.execute(
                text(
                    "INSERT INTO incidents (tenant_id, fingerprint, title, service, environment, priority, state) "
                    "VALUES (:t, 'fp', 'ErrorBudgetBurn', 'checkout', 'lab', 1, 'INVESTIGATING') "
                    "ON CONFLICT (tenant_id, fingerprint) WHERE closed_at IS NULL DO UPDATE SET title = EXCLUDED.title "
                    "RETURNING id"
                ),
                {"t": tenant},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO alerts (tenant_id, incident_id, fingerprint, status, labels) "
                    "VALUES (:t, :i, 'fp', :s, CAST(:l AS jsonb))"
                ),
                {"t": tenant, "i": incident_id, "s": status, "l": json.dumps(labels)},
            )
        if status == "firing":
            asyncio.run(self._diagnose(incident_id, TENANTS[tenant]))

    limited = 0  # how many injections the "model" sits out, rate limited

    async def _diagnose(self, incident_id: Any, top: str) -> None:
        engine = create_async_engine(self.database_url)
        if self.limited:
            async with engine.begin() as conn:
                await incidents.transition(
                    conn, incident_id, from_state="INVESTIGATING", to_state="ESCALATED", expected_version=1,
                    actor="test", reason=f"{MODEL_UNAVAILABLE}: rate_limited: HTTP 429: session limit · resets 8:50pm (UTC)",
                )  # fmt: skip
            await engine.dispose()
            return
        body = {
            "summary": "s",
            "root_causes": [{"service": top, "cause": "c", "confidence": 0.8, "evidence_ids": ["ev_0000000000000001"]}],
            "facts": [],
            "inferences": [],
            "hypotheses": [],
        }
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO blackboard (tenant_id, incident_id, kind, author, body) "
                    "SELECT tenant_id, id, 'diagnosis', 'synthesizer', CAST(:b AS jsonb) FROM incidents WHERE id = :id"
                ),
                {"id": incident_id, "b": json.dumps(body)},
            )
            await incidents.transition(
                conn, incident_id, from_state="INVESTIGATING", to_state="DIAGNOSED", expected_version=1,
                actor="test", reason=f"root cause candidate: {top}",
            )  # fmt: skip
        await engine.dispose()

    def inject(self, fault: Any) -> None:
        self.injected.append(fault.name)
        if fault.name == "payment-failure":
            for tenant in TENANTS:
                self._post(tenant, "firing")
            self.limited = max(0, self.limited - 1)

    def recover(self, fault: Any) -> bool:
        self.recovered.append(fault.name)
        if fault.name == "payment-failure":
            for tenant in TENANTS:
                self._post(tenant, "resolved")
        return True


@pytest.fixture
def sync_engine(migrated_url: str) -> Any:
    eng = create_engine(migrated_url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE executions, approvals, remediation_plans, blackboard, agent_runs, llm_calls, tool_calls, "
                "evidence, jobs, alerts, incidents, audit_log RESTART IDENTITY CASCADE"
            )
        )
    yield eng
    eng.dispose()


def test_a_scenario_with_a_distractor_is_run_and_observed_per_variant(
    sync_engine: Engine, migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evaluate.accept.time, "sleep", lambda s: None)  # the distractor's 90 s lead
    scenario = scenarios.get("payment-failure-after-checkout-release")
    lab = TwoTenantLab(sync_engine, migrated_url)
    diagnoser = Diagnoser(
        sync_engine,
        migrated_url,
        "lab",
        tmp_path / "agent",
        timeout_s=5,
        planning=False,
        variants={"lab-single": "single"},
    )
    results = evaluate.run(
        lab,  # type: ignore[arg-type]
        sync_engine,
        [scenario],
        diagnoser=diagnoser,
        remediator=None,
        database_url=migrated_url,
        tenant="lab",
        out=tmp_path,
        run_id="t1",
        log=lambda _: None,
        **FAST,
    )
    # The harmless checkout release first, then the fault; recovered the other way round.
    assert lab.injected == ["benign-release-checkout", "payment-failure"]
    assert lab.recovered == ["payment-failure", "benign-release-checkout"]

    assert [(o.scenario, o.variant, o.run) for o in results.observations] == [
        (scenario.id, "default", "t1"),
        (scenario.id, "single", "t1"),
    ]
    default, single = results.observations
    assert [i.top for i in default.incidents] == ["payment"] and [i.top for i in single.incidents] == ["checkout"]
    assert default.incidents[0].outcome == "diagnosed" and default.incidents[0].opened_s >= 0
    assert default.lab_ok and default.alerts == ["ErrorBudgetBurn/checkout"]

    # Written to disk as it goes; scored per variant: the single agent took the bait.
    on_disk = ResultFile.load(tmp_path / "results.json")
    scores = {s.variant: s for s in score_all(on_disk.observations)}
    assert scores["default"].top1 and not scores["single"].top1
    # Recordings: the default tenant's in <scenario>/, each variant's beside it.
    assert (tmp_path / "agent" / scenario.id / "incidents.json").exists()
    meta = json.loads((tmp_path / "agent" / scenario.id / "single" / "incidents.json").read_text())
    assert len(meta["incidents"]) == 1


def test_scenarios_are_dealt_out_over_shards_and_steps_keep_their_order() -> None:
    catalogue = scenarios.catalogue()
    parts = [evaluate.shard(catalogue, f"{i}/4") for i in range(1, 5)]
    assert sorted(s.id for p in parts for s in p) == sorted(s.id for s in catalogue)
    assert max(len(p) for p in parts) - min(len(p) for p in parts) <= 1
    with pytest.raises(ValueError):
        evaluate.shard(catalogue, "5/4")
    before, fault = evaluate.steps(scenarios.get("network-delay-with-ad-failure"))
    assert [(f.name, wait) for f, wait in before] == [("ad-failure", 60)] and fault.name == "network-delay"


def run_limited(
    sync_engine: Engine, migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deadline: float | None
) -> tuple[TwoTenantLab, ResultFile, list[float], list[str]]:
    monkeypatch.setattr(evaluate.accept.time, "sleep", lambda s: None)
    lab = TwoTenantLab(sync_engine, migrated_url)
    lab.limited = 1
    diagnoser = Diagnoser(sync_engine, migrated_url, "lab", tmp_path / "agent", timeout_s=5, planning=False)
    waits: list[float] = []
    lines: list[str] = []
    results = evaluate.run(
        lab,  # type: ignore[arg-type]
        sync_engine,
        [scenarios.get("payment-failure"), scenarios.get("bad-release")],
        diagnoser=diagnoser,
        remediator=None,
        database_url=migrated_url,
        tenant="lab",
        out=tmp_path,
        run_id="t1",
        log=lines.append,
        deadline=deadline,
        sleep=waits.append,
        alert_timeout=0.3,
        resolve_timeout=0.3,
        **FAST,
    )
    return lab, results, waits, lines


def test_a_scenario_that_ran_out_of_model_is_run_again_once_the_usage_window_reopens(
    sync_engine: Engine, migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab, results, waits, lines = run_limited(sync_engine, migrated_url, tmp_path, monkeypatch, deadline=None)
    assert lab.injected[:2] == ["payment-failure", "payment-failure"] and len(waits) == 1
    assert 0 < waits[0] <= 24 * 3600 + 60
    # Only the second attempt is observed, and it had a model.
    [first] = [o for o in results.observations if o.scenario == "payment-failure"]
    assert [i.top for i in first.incidents] == ["payment"]
    assert any("again" in x for x in lines)


def test_without_time_to_wait_the_run_stops_instead_of_measuring_the_limit(
    sync_engine: Engine, migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab, results, waits, lines = run_limited(
        sync_engine, migrated_url, tmp_path, monkeypatch, deadline=time.time() + 60
    )
    assert waits == [] and lab.injected == ["payment-failure"]
    [only] = results.observations
    assert only.incidents[0].model_failed
    assert any("stopping" in x and "bad-release" in x for x in lines)


def test_no_scenario_starts_once_the_run_has_used_its_token_budget(
    sync_engine: Engine, migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evaluate.accept.time, "sleep", lambda s: None)

    async def observe(database_url: str, ids: list[Any], cause_at: Any) -> list[IncidentObs]:
        return [IncidentObs(incident_id=str(i), service="checkout", priority=1, opened_s=1, outcome="diagnosed",
                            root_causes=["payment"], tokens=2_000_000) for i in ids]  # fmt: skip

    monkeypatch.setattr(evaluate, "_observe", observe)
    lab = TwoTenantLab(sync_engine, migrated_url)
    lines: list[str] = []
    evaluate.run(
        lab,  # type: ignore[arg-type]
        sync_engine,
        [scenarios.get("payment-failure"), scenarios.get("bad-release")],
        diagnoser=Diagnoser(sync_engine, migrated_url, "lab", tmp_path / "agent", timeout_s=5, planning=False),
        remediator=None,
        database_url=migrated_url,
        tenant="lab",
        out=tmp_path,
        run_id="t1",
        log=lines.append,
        token_budget=1_500_000,
        **FAST,
    )
    assert lab.injected == ["payment-failure"]
    assert any("2.0M tokens so far of 1.5M" in x for x in lines)
    assert any("stopping: the token budget is spent" in x and "bad-release" in x for x in lines)


def test_an_imported_run_replaces_the_recordings_and_a_rerun_variant_is_added_beside_them(tmp_path: Path) -> None:
    from scripts.lab.__main__ import main

    def run(run_id: str, variants: tuple[str, ...]) -> Path:
        src = tmp_path / run_id
        src.mkdir()
        (src / f"run-{run_id}.json").write_text("{}")
        for v in ("", *variants):
            d = src / "agent" / "pod-failure" / v
            d.mkdir(parents=True, exist_ok=True)
            for f in ("incidents.json", "llm.jsonl.gz", "tools.jsonl.gz"):
                (d / f).write_text(run_id)
        return src

    repo = tmp_path / "repo"
    full, rerun = run("1", ("single", "canary")), run("2", ("canary",))
    assert main(["eval-import", str(full), "--run", "1", "--repo", str(repo), "--baseline", "--variants", "single"]) == 0
    assert main(["eval-import", str(rerun), "--run", "2", "--repo", str(repo), "--add", "--variants", "canary"]) == 0
    rec = repo / "eval" / "recordings" / "pod-failure"
    assert (rec / "incidents.json").read_text() == "1"  # the default the canary is gated against
    assert (rec / "single" / "incidents.json").read_text() == "1"
    assert (rec / "canary" / "incidents.json").read_text() == "2"
    assert json.loads((repo / "eval" / "baseline.json").read_text())["results"] == ["eval/results/run-1.json"]
    assert sorted(p.name for p in (repo / "eval" / "results").iterdir()) == ["run-1.json", "run-2.json"]
