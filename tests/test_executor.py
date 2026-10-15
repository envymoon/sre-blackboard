"""The closed loop on Postgres (ADR-0007): approval -> executor -> verification -> RESOLVED.

The worker and the executor are separate Worker instances with separate job kinds, as they
are separate processes in production. Kubernetes is a fake seeded with the lab's real
checkout Deployment; Prometheus is a script of SLI values.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db import audit
from app.executor.job import make_execute
from app.executor.k8s import DeploymentWriter
from app.tools.sources import ReplaySource
from app.worker.handlers import default_handlers
from app.worker.investigate import AgentServices
from app.worker.runner import Worker
from tests.helpers.fake_k8s import TOKEN, FakeCluster, FakePrometheus
from tests.helpers.release_scripts import release_cassette, release_scripts
from tests.helpers.scripted_llm import ScriptedLLM
from tests.test_investigation import fire, states
from tests.test_remediation import TARGETS, as_user, transitions
from tests.test_worker import fast, wait_for

HEALTHY = [0.0]
STILL_FAILING = [0.42]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return fast(
        settings,
        remediation_targets=TARGETS,
        k8s_api_url="https://k8s.test",
        executor_k8s_token=SecretStr(TOKEN),
        executor_rollout_timeout_s=0.5,
        verify_settle_s=0.0,
        verify_interval_s=0.05,
        verify_healthy_checks=3,
        verify_timeout_s=1.0,
    )


async def run(
    engine: AsyncEngine,
    settings: Settings,
    *,
    done: set[str],
    cluster: FakeCluster,
    prometheus: FakePrometheus,
    executor: bool = True,
) -> None:
    services = AgentServices(
        ScriptedLLM(release_scripts()), ReplaySource(release_cassette()), prometheus=prometheus.client()
    )
    writer = DeploymentWriter.from_settings(settings, transport=cluster.transport())
    stop = asyncio.Event()
    workers = [Worker(engine, settings, default_handlers(services), worker_id="worker")]
    if executor:
        workers.append(Worker(engine, settings, {"execute": make_execute(writer)}, worker_id="executor-test"))
    tasks = [asyncio.create_task(w.run(stop)) for w in workers]

    async def finished() -> bool:
        return set((await states(engine)).values()) <= done

    try:
        await wait_for(finished, timeout=30)
    finally:
        stop.set()
        await asyncio.gather(*tasks)
        await writer.aclose()


async def approve(client: httpx.AsyncClient, settings: Settings, incident: UUID) -> None:
    plans = (await client.get(f"/api/v1/incidents/{incident}/plans", headers=as_user(settings, "v", "viewer"))).json()
    resp = await client.post(
        f"/api/v1/incidents/{incident}/approvals",
        json={"plan_hash": plans[-1]["plan_hash"], "decision": "approve", "comment": "go"},
        headers=as_user(settings, "alice", "approver"),
    )
    assert resp.status_code == 200, resp.text


async def approved(engine, settings, client, cluster: FakeCluster) -> UUID:
    """An incident whose rollback plan has been approved; the executor has not run yet."""
    ids = await fire(client, settings, "checkout")
    await run(
        engine,
        settings,
        done={"AWAITING_APPROVAL"},
        cluster=cluster,
        prometheus=FakePrometheus(HEALTHY),
        executor=False,
    )
    await approve(client, settings, ids["checkout"])
    return ids["checkout"]


async def executions(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list(await conn.execute(text("SELECT * FROM executions ORDER BY id")))


async def last_reason(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        reason: str = await conn.scalar(
            text(
                "SELECT payload->>'reason' FROM audit_log WHERE event = 'incident.transition' ORDER BY seq DESC LIMIT 1"
            )
        )
    return reason


async def test_the_bad_release_is_rolled_back_and_the_incident_resolves(engine, settings, client):
    """M5 acceptance, in miniature: alert -> diagnosis -> plan -> approval -> rollback -> SLO -> RESOLVED."""
    cluster = FakeCluster.checkout()
    assert (cluster.env("PAYMENT_ADDR"), cluster.revision) == ("payment:9999", 2)
    incident = await approved(engine, settings, client, cluster)
    prometheus = FakePrometheus([0.5, 0.0, 0.0, 0.0])
    await run(engine, settings, done={"RESOLVED"}, cluster=cluster, prometheus=prometheus)

    # The cluster runs revision 1's template again, as the newest revision, with a note why.
    assert cluster.env("PAYMENT_ADDR") == "payment:8080"
    assert cluster.revision == 3
    assert cluster.deployment["metadata"]["annotations"]["kubernetes.io/change-cause"].startswith(
        "sre-blackboard: plan "
    )
    assert [dry for dry, _ in cluster.patches] == [True, False]
    assert cluster.patches[1][1][0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "6170"}

    [run_row] = await executions(engine)
    assert run_row.status == "succeeded"
    assert run_row.before["revision"] == 2 and run_row.before["target_revision"] == 1
    assert run_row.after["rolled_out"] is True and run_row.after["revision"] == 3
    assert run_row.dry_run["accepted"] is True
    # The SLI was read until it had been healthy three times in a row.
    assert len(prometheus.queries) == 4 and 'service_name="checkout"' in prometheus.queries[0]

    moves = [(a, b) for a, b, _ in await transitions(engine)]
    assert moves[-3:] == [("AWAITING_APPROVAL", "EXECUTING"), ("EXECUTING", "VERIFYING"), ("VERIFYING", "RESOLVED")]
    trail = (await client.get(f"/api/v1/incidents/{incident}/audit", headers=as_user(settings, "v", "viewer"))).json()
    events = [e["event"] for e in trail if e["event"] != "incident.transition"]
    assert events == [
        "incident.created",
        "remediation.plan",
        "remediation.approved",
        "execution.started",
        "execution.succeeded",
        "verification.passed",
    ]
    actors = {e["event"]: e["actor"] for e in trail}
    assert actors["remediation.approved"] == "user:alice"
    assert actors["execution.started"].startswith("executor:executor-test")
    async with engine.connect() as conn:
        assert (await audit.verify(conn)).ok
        assert await conn.scalar(text("SELECT closed_at IS NOT NULL FROM incidents")) is True
    plans = (await client.get(f"/api/v1/incidents/{incident}/plans", headers=as_user(settings, "v", "viewer"))).json()
    assert plans[0]["executions"][0]["status"] == "succeeded"


async def test_the_worker_never_runs_an_execute_job(engine, settings, client):
    cluster = FakeCluster.checkout()
    await approved(engine, settings, client, cluster)
    services = AgentServices(ScriptedLLM(release_scripts()), ReplaySource(release_cassette()))
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(engine, settings, default_handlers(services), worker_id="w").run(stop))
    await asyncio.sleep(0.5)
    stop.set()
    await task
    assert cluster.patches == []
    assert (await states(engine)) == {"checkout": "EXECUTING"}


async def test_without_an_approval_the_executor_refuses(engine, settings, client):
    """As if a compromised worker had moved the incident on and queued the job itself."""
    cluster = FakeCluster.checkout()
    await fire(client, settings, "checkout")
    await run(
        engine,
        settings,
        done={"AWAITING_APPROVAL"},
        cluster=cluster,
        prometheus=FakePrometheus(HEALTHY),
        executor=False,
    )
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE incidents SET state = 'EXECUTING'"))
        plan_id = await conn.scalar(text("SELECT id FROM remediation_plans"))
        await conn.execute(
            text(
                "INSERT INTO jobs (tenant_id, incident_id, kind, payload, priority, max_attempts, sort_key) "
                "SELECT tenant_id, id, 'execute', CAST(:p AS jsonb), 1, 3, now() FROM incidents"
            ),
            {"p": f'{{"plan_id": {plan_id}}}'},
        )
    await run(engine, settings, done={"ESCALATED"}, cluster=cluster, prometheus=FakePrometheus(HEALTHY))
    assert cluster.patches == []
    assert await last_reason(engine) == "execution refused: it needs an approval and has none"


async def test_the_executor_applies_the_policy_again_before_acting(engine, settings, client):
    cluster = FakeCluster.checkout()
    await approved(engine, settings, client, cluster)
    closed = settings.model_copy(update={"remediation_change_window": "00:00-00:00"})
    await run(engine, closed, done={"ESCALATED"}, cluster=cluster, prometheus=FakePrometheus(HEALTHY))
    assert cluster.patches == []
    assert "the policy denies it now" in await last_reason(engine)
    assert "outside the change window" in await last_reason(engine)


async def test_an_interrupted_execution_is_never_sent_twice(engine, settings, client):
    """The intent row is there but nobody knows whether the patch landed: escalate, don't resend."""
    cluster = FakeCluster.checkout()
    await approved(engine, settings, client, cluster)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO executions (tenant_id, incident_id, plan_id, action_index, action, idempotency_key, "
                "status, executor) SELECT tenant_id, incident_id, id, 0, plan->'actions'->0, "
                "'plan:' || id || ':action:0', 'started', 'executor:crashed' FROM remediation_plans"
            )
        )
    await run(engine, settings, done={"ESCALATED"}, cluster=cluster, prometheus=FakePrometheus(HEALTHY))
    assert cluster.patches == []
    [row] = await executions(engine)
    assert row.status == "unknown"
    reason = await last_reason(engine)
    assert "outcome unknown after an interrupted attempt" in reason
    assert "roll the Deployment back to the revision it was on before" in reason


async def test_a_patch_without_an_answer_is_unknown_and_not_retried(engine, settings, client):
    cluster = FakeCluster.checkout()
    cluster.timeout_on_real_patch = True
    await approved(engine, settings, client, cluster)
    await run(engine, settings, done={"ESCALATED"}, cluster=cluster, prometheus=FakePrometheus(HEALTHY))
    assert cluster.real_patches() == 1
    [row] = await executions(engine)
    assert row.status == "unknown"
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT attempts FROM jobs WHERE kind = 'execute'")) == 1


async def test_a_deployment_changed_by_someone_else_is_not_overwritten(engine, settings, client):
    cluster = FakeCluster.checkout()
    cluster.changed_by_someone_else = True
    await approved(engine, settings, client, cluster)
    await run(engine, settings, done={"ESCALATED"}, cluster=cluster, prometheus=FakePrometheus(HEALTHY))
    assert cluster.env("PAYMENT_ADDR") == "payment:9999"
    [row] = await executions(engine)
    assert row.status == "failed" and "HTTP 422" in row.error


async def test_a_repair_that_does_not_bring_the_slo_back_escalates_with_the_way_back(engine, settings, client):
    cluster = FakeCluster.checkout()
    await approved(engine, settings, client, cluster)
    await run(engine, settings, done={"ESCALATED"}, cluster=cluster, prometheus=FakePrometheus(STILL_FAILING))
    reason = await last_reason(engine)
    assert reason.startswith("checkout error ratio over 1m still 0.42 ratio (healthy below 0.06)")
    assert "rollout_undo checkout can be undone: roll the Deployment back to the revision it was on before" in reason
    async with engine.connect() as conn:
        event = await conn.scalar(text("SELECT payload FROM audit_log WHERE event = 'verification.failed'"))
    assert len(event["samples"]) >= 3 and all(s["value"] == 0.42 for s in event["samples"])


async def test_no_data_is_not_a_recovery(engine, settings, client):
    cluster = FakeCluster.checkout()
    await approved(engine, settings, client, cluster)
    await run(engine, settings, done={"ESCALATED"}, cluster=cluster, prometheus=FakePrometheus([None]))
    assert "still no data" in await last_reason(engine)


def test_the_executor_will_not_start_without_its_own_identity(settings):
    with pytest.raises(RuntimeError, match="SRE_EXECUTOR_K8S_TOKEN"):
        DeploymentWriter.from_settings(settings.model_copy(update={"executor_k8s_token": None}))
