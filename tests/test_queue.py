"""Job queue behaviour against a real Postgres: ordering, aging, leases, fencing, retries."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.queue import jobs
from app.queue.jobs import LeaseLost

STEP = 60.0


async def enqueue(engine: AsyncEngine, priority: int, *, max_attempts: int = 3, key: str | None = None) -> int | None:
    async with engine.begin() as conn:
        return await jobs.enqueue(
            conn,
            tenant_id="t1",
            kind="noop",
            priority=priority,
            max_attempts=max_attempts,
            aging_step_s=STEP,
            idempotency_key=key,
        )


async def claim(engine: AsyncEngine, owner: str = "w1", ttl: float = 30.0, max_priority: int = 3) -> jobs.Job | None:
    async with engine.begin() as conn:
        return await jobs.claim(conn, owner=owner, lease_ttl_s=ttl, max_priority=max_priority)


async def expire_leases(engine: AsyncEngine) -> list[tuple[int, str]]:
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE status = 'running'"))
        return await jobs.reap(conn)


async def row(engine: AsyncEngine, job_id: int):
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id})).mappings().one()


async def test_higher_priority_first_within_an_aging_step(engine):
    for p in (3, 1, 0, 2):
        await enqueue(engine, p)
    order = []
    while (job := await claim(engine)) is not None:
        order.append(job.priority)
    assert order == [0, 1, 2, 3]


async def test_waiting_p3_is_not_starved_by_a_p0_flood(engine):
    """B1: the prototype blocked P1-P3 forever once any P0 had arrived."""
    p3 = await enqueue(engine, 3)
    # Pretend the P3 job has waited a little over 3 aging steps.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE jobs SET sort_key = sort_key - make_interval(secs => :s) WHERE id = :id"),
            {"s": 3 * STEP + 1, "id": p3},
        )
    for _ in range(20):
        await enqueue(engine, 0)
    first = await claim(engine)
    assert first is not None and first.id == p3


async def test_reserved_slot_only_takes_p0(engine):
    await enqueue(engine, 1)
    assert await claim(engine, max_priority=0) is None
    p0 = await enqueue(engine, 0)
    job = await claim(engine, max_priority=0)
    assert job is not None and job.id == p0


async def test_concurrent_claimers_never_get_the_same_job(engine):
    n_jobs, n_workers = 200, 10
    for i in range(n_jobs):
        await enqueue(engine, i % 4)
    claimed: list[int] = []

    async def worker(name: str) -> None:
        while True:
            async with engine.begin() as conn:
                job = await jobs.claim(conn, owner=name, lease_ttl_s=30)
                if job is None:
                    return
                await jobs.complete(conn, job)
            claimed.append(job.id)

    await asyncio.gather(*(worker(f"w{i}") for i in range(n_workers)))
    assert len(claimed) == n_jobs
    assert len(set(claimed)) == n_jobs


async def test_expired_lease_keeps_priority_and_place(engine):
    """B2: the prototype lost a job's priority when its lease expired."""
    job_id = await enqueue(engine, 1)
    before = await row(engine, job_id)
    first = await claim(engine)
    assert first is not None and first.attempts == 1 and first.fence == 1

    assert await expire_leases(engine) == [(job_id, "queued")]
    after = await row(engine, job_id)
    assert after["priority"] == before["priority"]
    assert after["sort_key"] == before["sort_key"]
    assert after["attempts"] == 1

    second = await claim(engine, owner="w2")
    assert second is not None and second.id == job_id
    assert second.attempts == 2 and second.fence == 2


async def test_zombie_worker_cannot_write_after_losing_its_lease(engine):
    await enqueue(engine, 0)
    zombie = await claim(engine, owner="zombie")
    assert zombie is not None
    await expire_leases(engine)
    owner = await claim(engine, owner="owner")
    assert owner is not None and owner.fence == zombie.fence + 1

    async with engine.begin() as conn:
        with pytest.raises(LeaseLost):
            await jobs.assert_fence(conn, zombie)
    async with engine.begin() as conn:
        with pytest.raises(LeaseLost):
            await jobs.complete(conn, zombie)
    async with engine.begin() as conn:
        assert not await jobs.heartbeat(conn, zombie, lease_ttl_s=30)

    async with engine.begin() as conn:
        await jobs.assert_fence(conn, owner)
        await jobs.complete(conn, owner)
    assert (await row(engine, owner.id))["status"] == "done"


async def test_failed_job_backs_off_then_dead_letters(engine):
    job_id = await enqueue(engine, 2, max_attempts=2)
    job = await claim(engine)
    assert job is not None
    async with engine.begin() as conn:
        status = await jobs.fail(conn, job, "boom", retryable=True, backoff_base_s=60, backoff_max_s=60)
    assert status == "queued"
    # Backing off: not claimable yet.
    assert await claim(engine) is None

    async with engine.begin() as conn:
        await conn.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id})
    job = await claim(engine)
    assert job is not None and job.attempts == 2
    async with engine.begin() as conn:
        status = await jobs.fail(conn, job, "boom again", retryable=True, backoff_base_s=1, backoff_max_s=1)
    assert status == "dead"
    dead = await row(engine, job_id)
    assert dead["last_error"] == "boom again"
    assert await claim(engine) is None


async def test_permanent_failure_dead_letters_immediately(engine):
    await enqueue(engine, 2, max_attempts=5)
    job = await claim(engine)
    assert job is not None
    async with engine.begin() as conn:
        assert await jobs.fail(conn, job, "bad input", retryable=False, backoff_base_s=1, backoff_max_s=1) == "dead"


async def test_reaper_dead_letters_a_job_out_of_attempts(engine):
    job_id = await enqueue(engine, 0, max_attempts=1)
    assert await claim(engine) is not None
    assert await expire_leases(engine) == [(job_id, "dead")]


async def test_release_does_not_count_as_an_attempt(engine):
    job_id = await enqueue(engine, 0)
    job = await claim(engine)
    assert job is not None
    async with engine.begin() as conn:
        await jobs.release(conn, job)
    released = await row(engine, job_id)
    assert released["status"] == "queued" and released["attempts"] == 0


async def test_enqueue_is_idempotent_per_key(engine):
    assert await enqueue(engine, 1, key="incident-1:triage") is not None
    assert await enqueue(engine, 1, key="incident-1:triage") is None
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM jobs"))).scalar_one() == 1


def test_backoff_grows_exponentially_and_is_capped():
    for attempt, cap in [(1, 2), (2, 4), (3, 8), (10, 300)]:
        delay = jobs.backoff_delay(attempt, base_s=2, max_s=300)
        assert cap / 2 <= delay <= cap
