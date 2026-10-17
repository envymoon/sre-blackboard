"""From diagnosis to a reviewed plan on Postgres (ADR-0007): planner, policy, approval deadline.

The investigation and the planner are scripted over the lab's bad-release recording; the
worker, queue, state machine, policy, plans table and audit chain are the real ones.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.security import mint_token
from app.db import audit
from app.tools.sources import ReplaySource
from app.worker.handlers import default_handlers
from app.worker.investigate import AgentServices
from app.worker.runner import Worker
from tests.helpers.release_scripts import nothing, release_cassette, release_scripts, undo
from tests.helpers.scripted_llm import ScriptedLLM
from tests.test_investigation import TENANT, fire, states
from tests.test_worker import ROOT, fast, wait_for

TARGETS = ["checkout", "payment", "frontend"]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The API and the worker read the same policy configuration, as they do in production."""
    return settings.model_copy(update={"remediation_targets": TARGETS})


def lab(settings: Settings, **overrides: Any) -> Settings:
    return fast(settings, **overrides)


async def run_until(engine: AsyncEngine, settings: Settings, llm: ScriptedLLM, *, done: set[str]) -> None:
    services = AgentServices(llm, ReplaySource(release_cassette()))
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(engine, settings, default_handlers(services), worker_id="test").run(stop))

    async def finished() -> bool:
        return set((await states(engine)).values()) <= done

    try:
        await wait_for(finished, timeout=30)
    finally:
        stop.set()
        await task


async def transitions(engine: AsyncEngine) -> list[tuple[str, str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT actor, payload FROM audit_log WHERE event = 'incident.transition' ORDER BY seq")
        )
        return [(r.payload["from"], r.payload["to"], r.actor) for r in rows]


async def plan_rows(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list(await conn.execute(text("SELECT * FROM remediation_plans ORDER BY id")))


async def test_a_bad_release_is_planned_reviewed_and_waits_for_an_approval(engine, settings, client):
    await fire(client, settings, "checkout")
    llm = ScriptedLLM(release_scripts())
    await run_until(engine, lab(settings), llm, done={"AWAITING_APPROVAL"})

    [plan] = await plan_rows(engine)
    assert (plan.verdict, plan.risk, plan.seq, plan.proposed_by) == ("require_approval", "R3", 1, "agent:planner")
    [action] = plan.plan["actions"]
    assert (action["action"], action["deployment"]) == ("rollout_undo", "checkout")
    assert all(c["ok"] for c in plan.checks), plan.checks
    assert plan.expires_at is not None
    # The planner looked at the rollout history itself before proposing (two steps).
    assert llm.calls("planner") == 2

    moves = [(a, b) for a, b, _ in await transitions(engine)]
    assert moves[-4:] == [
        ("INVESTIGATING", "DIAGNOSED"),
        ("DIAGNOSED", "PLANNED"),
        ("PLANNED", "SAFETY_REVIEWED"),
        ("SAFETY_REVIEWED", "AWAITING_APPROVAL"),
    ]
    async with engine.connect() as conn:
        jobs = {r.kind: r.status for r in await conn.execute(text("SELECT kind, status FROM jobs"))}
        event = await conn.scalar(text("SELECT payload FROM audit_log WHERE event = 'remediation.plan'"))
        assert (await audit.verify(conn)).ok
    assert jobs == {"triage": "done", "investigate": "done", "plan": "done", "approval_timeout": "queued"}
    assert event["plan_hash"] == plan.plan_hash and event["verdict"] == "require_approval"


async def test_a_plan_that_targets_another_service_is_denied(engine, settings, client):
    """As if a log line had talked the planner into "roll back payment": the policy says no."""
    await fire(client, settings, "checkout")
    await run_until(engine, lab(settings), ScriptedLLM(release_scripts(undo("payment"))), done={"ESCALATED"})
    [plan] = await plan_rows(engine)
    assert plan.verdict == "deny"
    failed = {c["name"]: c["detail"] for c in plan.checks if not c["ok"]}
    assert "payment is not the top root cause (checkout)" in failed["problem_host"]
    # It also cites a checkout release, which is no evidence for undoing payment.
    assert "rollout_history or deploy_diff result for payment" in failed["evidence"]
    assert (await transitions(engine))[-1][1:] == ("ESCALATED", "policy")


async def test_a_planner_that_finds_nothing_to_do_hands_over_with_its_reason(engine, settings, client):
    await fire(client, settings, "checkout")
    await run_until(engine, lab(settings), ScriptedLLM(release_scripts(nothing)), done={"ESCALATED"})
    async with engine.connect() as conn:
        reason = await conn.scalar(text("SELECT payload->>'reason' FROM audit_log WHERE payload->>'to' = 'ESCALATED'"))
    assert reason == "no safe automatic repair: the cause is outside the catalogue; check it by hand"


async def test_nothing_is_allowed_where_no_target_is_configured(engine, settings, client):
    await fire(client, settings, "checkout")
    await run_until(engine, fast(settings, remediation_targets=[]), ScriptedLLM(release_scripts()), done={"ESCALATED"})
    [plan] = await plan_rows(engine)
    assert plan.verdict == "deny"
    assert [c["name"] for c in plan.checks if not c["ok"]] == ["scope"]


async def test_an_approval_nobody_gives_in_time_escalates(engine, settings, client):
    await fire(client, settings, "checkout")
    await run_until(
        engine, lab(settings, remediation_approval_ttl_s=0.5), ScriptedLLM(release_scripts()), done={"ESCALATED"}
    )
    last = (await transitions(engine))[-1]
    assert last[:2] == ("AWAITING_APPROVAL", "ESCALATED") and last[2] == "policy"


async def test_a_plan_within_the_automatic_risk_goes_straight_to_the_executor(engine, settings, client):
    await fire(client, settings, "checkout")
    await run_until(
        engine, lab(settings, remediation_auto_max_risk="R3"), ScriptedLLM(release_scripts()), done={"EXECUTING"}
    )
    async with engine.connect() as conn:
        execute = (await conn.execute(text("SELECT status, payload FROM jobs WHERE kind = 'execute'"))).one()
    # Queued for the executor: the worker does not run execute jobs.
    assert execute.status == "queued"
    assert execute.payload == {"plan_id": (await plan_rows(engine))[0].id}


# --- approvals, revisions and the audit trail over the API ------------------------------------


def as_user(settings: Settings, name: str, *roles: str, tenant: str = TENANT) -> dict[str, str]:
    token = mint_token(settings, subject=name, tenant_id=tenant, roles=list(roles))
    return {"Authorization": f"Bearer {token}"}


async def waiting(engine, settings, client, **overrides: Any) -> tuple[UUID, dict[str, Any]]:
    """An incident whose plan waits for an approval, and that plan as the API shows it."""
    ids = await fire(client, settings, "checkout")
    await run_until(engine, lab(settings, **overrides), ScriptedLLM(release_scripts()), done={"AWAITING_APPROVAL"})
    incident = ids["checkout"]
    plans = (await client.get(f"/api/v1/incidents/{incident}/plans", headers=as_user(settings, "v", "viewer"))).json()
    return incident, plans[-1]


async def test_an_approver_approves_the_plan_they_reviewed(engine, settings, client):
    incident, plan = await waiting(engine, settings, client)
    url = f"/api/v1/incidents/{incident}/approvals"
    body = {"plan_hash": plan["plan_hash"], "decision": "approve", "comment": "rollback is safe"}

    assert (await client.post(url, json=body, headers=as_user(settings, "r", "responder"))).status_code == 403
    other_tenant = as_user(settings, "alice", "approver", tenant="other")
    assert (await client.post(url, json=body, headers=other_tenant)).status_code == 404
    stale = {**body, "plan_hash": "0" * 64}
    resp = await client.post(url, json=stale, headers=as_user(settings, "alice", "approver"))
    assert resp.status_code == 409 and "changed since you looked" in resp.json()["detail"]

    resp = await client.post(url, json=body, headers=as_user(settings, "alice", "approver"))
    assert resp.status_code == 200 and resp.json() == {"state": "EXECUTING"}
    again = await client.post(url, json=body, headers=as_user(settings, "bob", "approver"))
    assert again.status_code == 409

    [shown] = (await client.get(f"/api/v1/incidents/{incident}/plans", headers=as_user(settings, "v", "viewer"))).json()
    assert shown["approval"]["approver"] == "user:alice" and shown["approval"]["decision"] == "approve"
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT status FROM jobs WHERE kind = 'execute'")) == "queued"

    trail = (await client.get(f"/api/v1/incidents/{incident}/audit", headers=as_user(settings, "v", "viewer"))).json()
    events = [e["event"] for e in trail]
    assert events.index("remediation.plan") < events.index("remediation.approved")
    approved = next(e for e in trail if e["event"] == "remediation.approved")
    assert approved["actor"] == "user:alice" and approved["payload"]["plan_hash"] == plan["plan_hash"]


async def test_a_rejection_hands_the_incident_to_a_human(engine, settings, client):
    incident, plan = await waiting(engine, settings, client)
    body = {"plan_hash": plan["plan_hash"], "decision": "reject", "comment": "wait for the release owner"}
    resp = await client.post(
        f"/api/v1/incidents/{incident}/approvals", json=body, headers=as_user(settings, "alice", "approver")
    )
    assert resp.json() == {"state": "ESCALATED"}
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM jobs WHERE kind = 'execute'")) == 0


async def test_a_revised_plan_voids_earlier_approvals_and_its_author_cannot_approve_it(engine, settings, client):
    incident, first = await waiting(engine, settings, client)
    revised = {**first["plan"], "summary": "Same rollback, but to revision 1 explicitly."}
    revised["actions"] = [{**revised["actions"][0], "to_revision": 1}]

    resp = await client.post(
        f"/api/v1/incidents/{incident}/plans", json=revised, headers=as_user(settings, "carol", "approver")
    )
    assert resp.status_code == 201, resp.text
    second = resp.json()
    assert second["state"] == "AWAITING_APPROVAL" and second["plan_hash"] != first["plan_hash"]

    url = f"/api/v1/incidents/{incident}/approvals"
    old = {"plan_hash": first["plan_hash"], "decision": "approve"}
    assert (await client.post(url, json=old, headers=as_user(settings, "alice", "approver"))).status_code == 409
    mine = {"plan_hash": second["plan_hash"], "decision": "approve"}
    resp = await client.post(url, json=mine, headers=as_user(settings, "carol", "approver"))
    assert resp.status_code == 403 and "proposed it" in resp.json()["detail"]
    resp = await client.post(url, json=mine, headers=as_user(settings, "alice", "approver"))
    assert resp.json() == {"state": "EXECUTING"}

    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT payload->>'plan_id' FROM jobs WHERE kind = 'execute'")) == str(
            second["plan_id"]
        )
    moves = [(a, b) for a, b, _ in await transitions(engine)]
    assert ("AWAITING_APPROVAL", "PLANNED") in moves


async def test_a_revision_goes_through_the_same_policy(engine, settings, client):
    incident, first = await waiting(engine, settings, client)
    wrong = {**first["plan"], "actions": [{**first["plan"]["actions"][0], "deployment": "frontend"}]}
    resp = await client.post(
        f"/api/v1/incidents/{incident}/plans", json=wrong, headers=as_user(settings, "carol", "responder")
    )
    assert resp.json()["verdict"] == "deny" and resp.json()["state"] == "ESCALATED"
    outside = {**first["plan"], "actions": [{**first["plan"]["actions"][0], "action": "delete_namespace"}]}
    resp = await client.post(
        f"/api/v1/incidents/{incident}/plans", json=outside, headers=as_user(settings, "carol", "responder")
    )
    assert resp.status_code in (409, 422)


async def test_an_approval_after_the_deadline_is_refused(engine, settings, client):
    incident, plan = await waiting(engine, settings, client, remediation_approval_ttl_s=0.3)
    await asyncio.sleep(0.4)
    resp = await client.post(
        f"/api/v1/incidents/{incident}/approvals",
        json={"plan_hash": plan["plan_hash"], "decision": "approve"},
        headers=as_user(settings, "alice", "approver"),
    )
    assert resp.status_code == 409 and "closed" in resp.json()["detail"]


async def test_plans_and_approvals_cannot_be_rewritten(engine, settings, client):
    incident, plan = await waiting(engine, settings, client)
    await client.post(
        f"/api/v1/incidents/{incident}/approvals",
        json={"plan_hash": plan["plan_hash"], "decision": "approve"},
        headers=as_user(settings, "alice", "approver"),
    )
    for statement in (
        "UPDATE remediation_plans SET verdict = 'allow'",
        "DELETE FROM remediation_plans",
        "UPDATE approvals SET approver = 'user:mallory'",
        "DELETE FROM approvals",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement))


async def test_the_audit_chain_can_be_verified_over_the_api_and_the_cli(engine, settings, client):
    await waiting(engine, settings, client)
    url = "/api/v1/audit/verify"
    assert (await client.get(url, headers=as_user(settings, "v", "approver"))).status_code == 403
    ok = (await client.get(url, headers=as_user(settings, "root", "admin"))).json()
    assert ok["ok"] and ok["checked"] >= 8
    env = {**os.environ, "SRE_DATABASE_URL": settings.database_url}
    cli = [sys.executable, "-m", "app.db.audit", "verify"]
    assert subprocess.run(cli, cwd=ROOT, env=env, capture_output=True).returncode == 0

    async with engine.begin() as conn:
        seq = await conn.scalar(text("SELECT seq FROM audit_log WHERE event = 'remediation.plan'"))
        await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete"))
        await conn.execute(text("UPDATE audit_log SET actor = 'user:mallory' WHERE event = 'remediation.plan'"))
        await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update_delete"))
    bad = (await client.get(url, headers=as_user(settings, "root", "admin"))).json()
    assert not bad["ok"] and bad["first_bad_seq"] == seq
    broken = subprocess.run(cli, cwd=ROOT, env=env, capture_output=True, text=True)
    assert broken.returncode == 1 and f"seq {bad['first_bad_seq']}" in broken.stdout
