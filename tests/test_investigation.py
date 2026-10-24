"""The investigation end to end on Postgres: alert -> triage -> investigate -> DIAGNOSED.

The model is scripted and the tools replay the lab's payment-failure recording, so this runs
the real worker, queue, state machine, evidence store, checkpoints and API.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents import recording
from app.agents.investigation import AgentConfig
from app.core.config import Settings
from app.core.security import mint_token
from app.db import audit, investigations
from app.db import incidents as incidents_repo
from app.llm.types import LLMError
from app.tools.sources import ReplaySource
from app.worker.handlers import default_handlers
from app.worker.investigate import AgentServices
from app.worker.runner import Worker
from tests.helpers.payment_scripts import payment_cassette, payment_scripts
from tests.helpers.scripted_llm import ScriptedLLM
from tests.test_worker import ROOT, fast, wait_for

TENANT = "lab"


def auth(settings: Settings, role: str, tenant: str = TENANT) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_token(settings, subject=role, tenant_id=tenant, roles=[role])}"}


def alert(service: str) -> dict[str, Any]:
    return {
        "status": "firing",
        "labels": {
            "alertname": "ErrorBudgetBurn",
            "service_name": service,
            "environment": "lab",
            "severity": "critical",
            "burn": "fast",
            "slo": "availability",
        },
        "annotations": {"summary": f"{service} is burning its error budget"},
        "startsAt": "2026-10-15T04:05:00Z",
    }


async def fire(client: httpx.AsyncClient, settings: Settings, *services: str) -> dict[str, UUID]:
    body = {"version": "4", "status": "firing", "receiver": "sre", "alerts": [alert(s) for s in services]}
    resp = await client.post("/api/v1/alerts", json=body, headers=auth(settings, "ingest"))
    assert resp.status_code == 202, resp.text
    incidents = (await client.get("/api/v1/incidents", headers=auth(settings, "viewer"))).json()
    return {i["service"]: UUID(i["id"]) for i in incidents}


async def states(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as conn:
        return {r.service: r.state for r in await conn.execute(text("SELECT service, state FROM incidents"))}


async def run_until(engine: AsyncEngine, settings: Settings, llm: ScriptedLLM, *, done: set[str]) -> None:
    """Run a worker until every incident is in one of ``done``. Diagnosis only: no repair planning."""
    settings = settings.model_copy(update={"auto_remediate": False})
    services = AgentServices(llm, ReplaySource(payment_cassette()))
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(engine, settings, default_handlers(services), worker_id="test").run(stop))

    async def finished() -> bool:
        return set((await states(engine)).values()) <= done

    try:
        await wait_for(finished, timeout=30)
    finally:
        stop.set()
        await task


async def test_an_alert_ends_in_a_diagnosis_with_openable_evidence(engine, settings, client):
    ids = await fire(client, settings, "checkout", "payment")
    llm = ScriptedLLM(payment_scripts())
    await run_until(engine, fast(settings), llm, done={"DIAGNOSED"})
    checkout = ids["checkout"]

    dx = (await client.get(f"/api/v1/incidents/{checkout}/diagnosis", headers=auth(settings, "viewer"))).json()
    assert dx["state"] == "DIAGNOSED"
    assert dx["diagnosis"]["root_causes"][0]["service"] == "payment"
    assert {a: r["status"] for a, r in dx["agents"].items()} == dict.fromkeys(
        ["coordinator", "investigator.metrics", "investigator.logs_traces", "investigator.changes", "synthesizer"],
        "done",
    )
    assert len(dx["findings"]) == 4
    # One investigation: coordinator 1, three investigators 2 steps each, synthesizer 1.
    assert dx["usage"]["calls"] == 8 and dx["usage"]["errors"] == 0 and dx["usage"]["tokens"] == 8000
    assert all(r["prompt_version"].startswith(name) for name, r in dx["agents"].items())

    # Every cited evidence id opens, belongs to this incident, and its hash checks out.
    for ev in {e for rc in dx["diagnosis"]["root_causes"] for e in rc["evidence_ids"]}:
        row = (await client.get(f"/api/v1/evidence/{ev}", headers=auth(settings, "viewer"))).json()
        assert row["incident_id"] == str(checkout) and row["source"] == "replay" and row["result"] is not None
        # ... and is invisible to another tenant.
        other = await client.get(f"/api/v1/evidence/{ev}", headers=auth(settings, "viewer", tenant="other"))
        assert other.status_code == 404

    report = await client.get(f"/api/v1/incidents/{checkout}/report", headers=auth(settings, "viewer"))
    assert report.headers["content-type"].startswith("text/markdown")
    assert "# ErrorBudgetBurn on checkout" in report.text and "| 1 | payment |" in report.text

    # The coordinator was told about the payment incident opened alongside.
    coordinator_task = next(
        r for r in llm.requests if r.agent == "coordinator" and "on service checkout" in r.turns[0]["text"]
    )
    assert "ErrorBudgetBurn on payment (P1)" in coordinator_task.turns[0]["text"]

    async with engine.connect() as conn:
        moves = [
            (r.payload["from"], r.payload["to"])
            for r in await conn.execute(
                text("SELECT payload FROM audit_log WHERE event = 'incident.transition' AND subject = :s ORDER BY seq"),
                {"s": str(checkout)},
            )
        ]
        assert moves == [("RECEIVED", "TRIAGED"), ("TRIAGED", "INVESTIGATING"), ("INVESTIGATING", "DIAGNOSED")]
        assert (await audit.verify(conn)).ok


async def test_diagnosis_of_another_tenants_incident_is_not_found(engine, settings, client):
    ids = await fire(client, settings, "checkout")
    resp = await client.get(
        f"/api/v1/incidents/{ids['checkout']}/diagnosis", headers=auth(settings, "viewer", tenant="other")
    )
    assert resp.status_code == 404


async def test_an_exhausted_budget_escalates(engine, settings, client):
    await fire(client, settings, "checkout")
    await run_until(
        engine, fast(settings, llm_incident_token_budget=2500), ScriptedLLM(payment_scripts()), done={"ESCALATED"}
    )
    async with engine.connect() as conn:
        reason = await conn.scalar(text("SELECT payload->>'reason' FROM audit_log WHERE payload->>'to' = 'ESCALATED'"))
    assert reason.startswith("token budget exhausted")


async def test_a_model_that_stays_down_escalates_after_the_last_attempt(engine, settings, client):
    await fire(client, settings, "checkout")
    llm = ScriptedLLM(payment_scripts())
    llm.fail["coordinator"] = LLMError("overloaded", "529", retryable=True)
    await run_until(engine, fast(settings, queue_max_attempts=2), llm, done={"ESCALATED"})
    assert llm.calls("coordinator") == 2  # one per job attempt; the job retried once


def start_agent_worker(settings: Settings, **env: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "tests.helpers.agent_worker"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "SRE_DATABASE_URL": settings.database_url,
            "SRE_WORKER_POLL_INTERVAL_S": "0.05",
            "SRE_QUEUE_LEASE_TTL_S": "1",
            **env,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


async def llm_calls_by_agent(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT agent, count(*) AS n FROM llm_calls GROUP BY agent"))
        return {r.agent: r.n for r in rows}


async def test_a_worker_killed_mid_investigation_is_taken_over_without_asking_finished_agents_again(
    engine, settings, client
):
    """M4 acceptance: kill the worker while the synthesizer runs; another worker finishes the
    incident, and no finished agent is asked again."""
    await fire(client, settings, "checkout")
    proc = start_agent_worker(settings, AGENT_WORKER_HANG="synthesizer")
    try:

        async def synthesizer_running() -> bool:
            async with engine.connect() as conn:
                return bool(await conn.scalar(text("SELECT count(*) FROM agent_runs WHERE agent = 'synthesizer'")))

        await wait_for(synthesizer_running, timeout=30)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        proc.kill()
    assert (await states(engine)) == {"checkout": "INVESTIGATING"}
    before = await llm_calls_by_agent(engine)
    assert before == {
        "coordinator": 1,
        "investigator.metrics": 2,
        "investigator.logs_traces": 2,
        "investigator.changes": 2,
    }

    llm = ScriptedLLM(payment_scripts())
    await run_until(engine, fast(settings), llm, done={"DIAGNOSED"})
    assert {r.agent for r in llm.requests} == {"synthesizer"}
    assert await llm_calls_by_agent(engine) == {**before, "synthesizer": 1}
    async with engine.connect() as conn:
        findings = await conn.scalar(text("SELECT count(*) FROM blackboard WHERE kind = 'finding'"))
        attempts = await conn.scalar(text("SELECT attempts FROM jobs WHERE kind = 'investigate'"))
    assert findings == 4 and attempts == 2


async def test_an_exported_investigation_replays_to_the_same_diagnosis(engine, settings, client, tmp_path):
    ids = await fire(client, settings, "checkout", "payment")
    await run_until(engine, fast(settings), ScriptedLLM(payment_scripts()), done={"DIAGNOSED"})
    rec = await recording.export(engine, list(ids.values()), meta={"test": True})
    rec.dump(tmp_path / "rec")
    rec = recording.Recording.load(tmp_path / "rec")
    assert {r["service"] for r in rec.incidents} == {"checkout", "payment"}
    assert {e["agent"] for e in rec.tools.entries} == {
        "investigator.metrics",
        "investigator.logs_traces",
        "investigator.changes",
    }
    assert {e["incident"] for e in rec.tools.entries} == {str(i) for i in ids.values()}

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE blackboard, agent_runs, llm_calls, tool_calls, evidence, jobs, alerts, incidents, audit_log CASCADE"
            )
        )
    await recording.load_incidents(engine, rec)
    config = AgentConfig(model_for=lambda role: "claude-haiku-5-5", max_steps=settings.agent_max_steps)
    for service, incident_id in ids.items():
        outcome = await recording.replay(engine, rec, incident_id, config)
        expected = rec.expected[str(incident_id)]
        assert outcome.diagnosis is not None and outcome.diagnosis.model_dump() == expected["diagnosis"], service
        assert {a: "done" for a in outcome.agents} == expected["agents"]
    # Nothing was asked that the recording could not answer.
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM tool_calls WHERE error_code = 'replay_miss'")) == 0
        assert await conn.scalar(text("SELECT count(DISTINCT source) FROM evidence")) == 1


def test_a_too_large_answer_is_recorded_and_a_local_error_is_not():
    """too_large is raised after the backend answered: the replay must be told it again."""
    row = SimpleNamespace(
        incident_id=uuid4(),
        agent="investigator.logs_traces",
        tool="traces_search",
        args={"service": "checkout"},
        status="error",
        error_code="too_large",
        error="the result is 300 KiB, over the 256 KiB limit; narrow the query",
        created_at=datetime(2026, 10, 15, tzinfo=UTC),
        result=None,
        fetched_at=None,
    )
    entry = recording.tool_entry(row)
    assert entry is not None and entry["error"] == {"code": "too_large", "message": row.error}
    assert entry["incident"] == str(row.incident_id)
    assert recording.tool_entry(SimpleNamespace(**{**vars(row), "error_code": "invalid_args"})) is None


def test_each_incident_replays_with_its_own_tool_answers():
    a, b = UUID(int=1), UUID(int=2)
    rec = recording.Recording()
    rec.tools.entries = [
        {"incident": str(a), "tool": "prom_query", "args": {}, "raw": 1},
        {"incident": str(b), "tool": "prom_query", "args": {}, "raw": 2},
        {"tool": "prom_query", "args": {}, "raw": 3},  # recorded before entries were tagged
    ]
    assert [e["raw"] for e in recording.incident_tools(rec, a).entries] == [1, 3]
    assert [e["raw"] for e in recording.incident_tools(rec, b).entries] == [2, 3]


async def test_an_incident_is_told_only_about_incidents_open_before_it(engine, settings, client):
    """A later incident may not exist yet when this one is investigated: never mention it, replay or not."""
    first = await fire(client, settings, "payment")
    second = await fire(client, settings, "checkout")
    async with engine.connect() as conn:
        views = {
            s: await investigations.incident_view(conn, await incidents_repo.get(conn, second[s], tenant_id=None))
            for s in ("payment", "checkout")
        }
    assert first["payment"] == second["payment"]
    assert [r["service"] for r in views["checkout"].related] == ["payment"]
    assert views["payment"].related == []
