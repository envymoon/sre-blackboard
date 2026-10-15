"""Worker behaviour: handlers, retries, lease loss, reserved P0 slots, real process kill/stop."""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db import audit, incidents
from app.queue import jobs
from app.worker.handlers import triage
from app.worker.runner import Handler, JobContext, Worker

ROOT = Path(__file__).resolve().parent.parent


def fast(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(
        update={
            "queue_lease_ttl_s": 1.0,
            "queue_backoff_base_s": 0.01,
            "queue_backoff_max_s": 0.02,
            "worker_poll_interval_s": 0.05,
            "worker_reap_interval_s": 0.1,
            "worker_shutdown_grace_s": 1.0,
            **overrides,
        }
    )


async def enqueue(engine: AsyncEngine, kind: str, *, priority: int = 2, max_attempts: int = 3, **kw) -> int:
    async with engine.begin() as conn:
        job_id = await jobs.enqueue(
            conn, tenant_id="t1", kind=kind, priority=priority, max_attempts=max_attempts, aging_step_s=60, **kw
        )
    assert job_id is not None
    return job_id


async def job_row(engine: AsyncEngine, job_id: int):
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id})).mappings().one()


async def wait_for(check: Callable[[], Awaitable[bool] | bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not await _resolve(check()):
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.05)


async def run_worker_until(
    engine: AsyncEngine, settings: Settings, handlers: dict[str, Handler], check: Callable[[], Awaitable[bool]]
) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(engine, settings, handlers, worker_id="test").run(stop))
    try:
        await wait_for(check)
    finally:
        stop.set()
        await task


async def _resolve(value: Awaitable[bool] | bool) -> bool:
    return await value if inspect.isawaitable(value) else value


def status_is(engine: AsyncEngine, job_id: int, status: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await job_row(engine, job_id))["status"] == status

    return check


async def test_triage_moves_incident_to_triaged(engine, settings):
    async with engine.begin() as conn:
        incident_id, created = await incidents.open_or_merge(
            conn,
            tenant_id="t1",
            fingerprint="fp1",
            title="HighErrorRate",
            service="checkout",
            environment="prod",
            priority=1,
            labels={},
        )
    assert created
    job_id = await enqueue(engine, "triage", incident_id=incident_id)

    await run_worker_until(engine, fast(settings), {"triage": triage}, status_is(engine, job_id, "done"))

    async with engine.connect() as conn:
        incident = await incidents.get(conn, incident_id, tenant_id="t1")
        assert incident is not None
        assert (incident["state"], incident["version"]) == ("TRIAGED", 2)
        result = await audit.verify(conn)
        assert result.ok and result.checked == 1
        # ... and queued the investigation, with the incident's priority.
        follow_up = (
            await conn.execute(
                text("SELECT kind, priority, status FROM jobs WHERE incident_id = :id AND id <> :job"),
                {"id": incident_id, "job": job_id},
            )
        ).one()
        assert tuple(follow_up) == ("investigate", 1, "queued")


async def test_failing_handler_is_retried_then_dead_lettered(engine, settings):
    calls = 0

    async def broken(ctx: JobContext) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("downstream unavailable")

    job_id = await enqueue(engine, "broken", max_attempts=3)
    await run_worker_until(engine, fast(settings), {"broken": broken}, status_is(engine, job_id, "dead"))

    row = await job_row(engine, job_id)
    assert calls == 3 and row["attempts"] == 3
    assert "downstream unavailable" in row["last_error"]
    async with engine.connect() as conn:
        events = (await conn.execute(text("SELECT event FROM audit_log"))).scalars().all()
    assert events == ["job.dead"]


async def test_flaky_handler_succeeds_on_retry(engine, settings):
    calls = 0

    async def flaky(ctx: JobContext) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first try fails")

    job_id = await enqueue(engine, "flaky")
    await run_worker_until(engine, fast(settings), {"flaky": flaky}, status_is(engine, job_id, "done"))
    assert (await job_row(engine, job_id))["attempts"] == 2


async def test_unknown_job_kind_is_dead_lettered_without_retry(engine, settings):
    job_id = await enqueue(engine, "no-such-kind", max_attempts=5)
    await run_worker_until(engine, fast(settings), {}, status_is(engine, job_id, "dead"))
    assert (await job_row(engine, job_id))["attempts"] == 1


async def test_handler_is_cancelled_when_its_lease_is_taken(engine, settings):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(ctx: JobContext) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    job_id = await enqueue(engine, "slow")
    stop = asyncio.Event()
    task = asyncio.create_task(Worker(engine, fast(settings), {"slow": slow}, worker_id="w1").run(stop))
    await asyncio.wait_for(started.wait(), 5)
    # Someone else now owns the job (as if the reaper had requeued it and another worker claimed it).
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE jobs SET fence = fence + 1, lease_owner = 'w2' WHERE id = :id"), {"id": job_id})
    await asyncio.wait_for(cancelled.wait(), 5)
    stop.set()
    await task
    row = await job_row(engine, job_id)
    assert row["status"] == "running" and row["lease_owner"] == "w2"


async def test_last_free_slot_is_reserved_for_p0(engine, settings):
    running: list[int] = []

    async def hold(ctx: JobContext) -> None:
        running.append(ctx.job.priority)
        await asyncio.sleep(30)

    await enqueue(engine, "hold", priority=2)
    await enqueue(engine, "hold", priority=2)
    stop = asyncio.Event()
    worker = Worker(engine, fast(settings, worker_concurrency=2, worker_reserved_p0_slots=1), {"hold": hold})
    task = asyncio.create_task(worker.run(stop))
    try:
        await wait_for(lambda: len(running) >= 1)
        await asyncio.sleep(0.3)
        assert running == [2], "the second slot must stay free for P0"
        await enqueue(engine, "hold", priority=0)
        await wait_for(lambda: len(running) == 2)
        assert running == [2, 0]
    finally:
        stop.set()
        await task


def start_worker_process(settings: Settings, **env: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "tests.helpers.sleep_worker"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "SRE_DATABASE_URL": settings.database_url,
            "SRE_WORKER_POLL_INTERVAL_S": "0.05",
            **env,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


async def test_sigterm_releases_the_running_job_and_exits_cleanly(engine, settings):
    """B8: the prototype worker's main() only printed a line."""
    job_id = await enqueue(engine, "sleep", payload={"seconds": 30})
    proc = start_worker_process(settings, SRE_WORKER_SHUTDOWN_GRACE_S="0.5")
    try:
        await wait_for(status_is(engine, job_id, "running"), timeout=20)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
    finally:
        proc.kill()
    row = await job_row(engine, job_id)
    assert row["status"] == "queued" and row["attempts"] == 0 and row["lease_owner"] is None


async def test_killed_worker_job_is_taken_over_after_lease_expiry(engine, settings):
    job_id = await enqueue(engine, "sleep", payload={"seconds": 1})
    proc = start_worker_process(settings, SRE_QUEUE_LEASE_TTL_S="1")
    try:
        await wait_for(status_is(engine, job_id, "running"), timeout=20)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        proc.kill()

    async def sleep(ctx: JobContext) -> None:
        await asyncio.sleep(float(ctx.job.payload["seconds"]))

    await run_worker_until(engine, fast(settings), {"sleep": sleep}, status_is(engine, job_id, "done"))
    row = await job_row(engine, job_id)
    assert row["attempts"] == 2 and row["fence"] == 2


async def test_triage_only_mode_queues_no_investigation(engine, settings):
    async with engine.begin() as conn:
        incident_id, _ = await incidents.open_or_merge(
            conn, tenant_id="t1", fingerprint="fp2", title="T", service="s", environment="prod", priority=2, labels={}
        )
    job_id = await enqueue(engine, "triage", incident_id=incident_id)
    off = fast(settings, auto_investigate=False)
    await run_worker_until(engine, off, {"triage": triage}, status_is(engine, job_id, "done"))
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM jobs WHERE kind = 'investigate'")) == 0
