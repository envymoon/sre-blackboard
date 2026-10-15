"""From diagnosis to a reviewed plan on Postgres (ADR-0007): planner, policy, approval deadline.

The investigation and the planner are scripted over the lab's bad-release recording; the
worker, queue, state machine, policy, plans table and audit chain are the real ones.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db import audit
from app.tools.sources import ReplaySource
from app.worker.handlers import default_handlers
from app.worker.investigate import AgentServices
from app.worker.runner import Worker
from tests.helpers.release_scripts import nothing, release_cassette, release_scripts, undo
from tests.helpers.scripted_llm import ScriptedLLM
from tests.test_investigation import fire, states
from tests.test_worker import fast, wait_for

TARGETS = ["checkout", "payment", "frontend"]


def lab(settings: Settings, **overrides: Any) -> Settings:
    return fast(settings, remediation_targets=TARGETS, **overrides)


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
    await run_until(engine, fast(settings), ScriptedLLM(release_scripts()), done={"ESCALATED"})
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
