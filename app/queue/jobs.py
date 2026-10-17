"""Postgres-backed job queue.

Every function takes an open connection; the caller owns the transaction. A job moves
through ``queued -> running -> done``. A failed run goes back to ``queued`` after a
backoff, or to ``dead`` once it has used ``max_attempts``.

Claiming uses ``FOR UPDATE SKIP LOCKED``: concurrent workers each lock a different row
instead of waiting on the same one. Each claim increments ``fence``. A worker that lost
its lease (it stalled, the reaper requeued the job, someone else claimed it) still holds
the old fence, so all of its writes are rejected.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection


class LeaseLost(Exception):
    """The job's fence moved on: another worker owns it now, or it is no longer running."""


@dataclass(frozen=True)
class Job:
    id: int
    tenant_id: str
    incident_id: UUID | None
    kind: str
    payload: dict[str, Any]
    priority: int
    attempts: int
    max_attempts: int
    fence: int
    lease_owner: str

    @classmethod
    def from_row(cls, row: RowMapping) -> Job:
        return cls(**{f: row[f] for f in cls.__dataclass_fields__})


async def enqueue(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    kind: str,
    priority: int,
    max_attempts: int,
    aging_step_s: float,
    incident_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    delay_s: float = 0.0,
) -> int | None:
    """Add a job. Returns its id, or None if a job with the same idempotency key exists.

    ``delay_s`` holds it back (an approval deadline, a settle time before verifying); its place
    in line is still counted from now.
    """
    row = await conn.execute(
        text(
            """
            INSERT INTO jobs (tenant_id, incident_id, kind, payload, idempotency_key,
                              priority, max_attempts, sort_key, run_after)
            VALUES (:tenant_id, :incident_id, :kind, CAST(:payload AS jsonb), :key,
                    :priority, :max_attempts, now() + make_interval(secs => :priority * :step),
                    now() + make_interval(secs => :delay))
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "kind": kind,
            "payload": _json(payload or {}),
            "key": idempotency_key,
            "priority": priority,
            "max_attempts": max_attempts,
            "step": aging_step_s,
            "delay": delay_s,
        },
    )
    return row.scalar_one_or_none()


async def claim(
    conn: AsyncConnection,
    *,
    owner: str,
    lease_ttl_s: float,
    max_priority: int = 3,
    kinds: Sequence[str] | None = None,
) -> Job | None:
    """Lease the next runnable job, or return None if there is none.

    Order is ``sort_key``: enqueue time plus priority * aging step. Within one step P0 goes
    first, but a P3 that has waited 3 steps beats any newly arrived P0, so nothing starves.
    ``kinds`` limits the claim to the jobs a process can run: the executor, which alone holds
    write access to the cluster, is the only one that claims ``execute`` jobs.
    """
    result = await conn.execute(
        text(
            """
            WITH next AS (
                SELECT id FROM jobs
                WHERE status = 'queued' AND run_after <= now() AND priority <= :max_priority
                  AND (CAST(:kinds AS text[]) IS NULL OR kind = ANY(CAST(:kinds AS text[])))
                ORDER BY sort_key, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs AS j
            SET status = 'running',
                lease_owner = :owner,
                lease_until = now() + make_interval(secs => :ttl),
                fence = j.fence + 1,
                attempts = j.attempts + 1,
                updated_at = now()
            FROM next
            WHERE j.id = next.id
            RETURNING j.*
            """
        ),
        {"owner": owner, "ttl": lease_ttl_s, "max_priority": max_priority, "kinds": list(kinds) if kinds else None},
    )
    row = result.mappings().one_or_none()
    return Job.from_row(row) if row else None


async def heartbeat(conn: AsyncConnection, job: Job, *, lease_ttl_s: float) -> bool:
    """Extend the lease. False means the lease is gone and the worker must stop the job."""
    result = await conn.execute(
        text(
            """
            UPDATE jobs SET lease_until = now() + make_interval(secs => :ttl), updated_at = now()
            WHERE id = :id AND fence = :fence AND status = 'running'
            RETURNING id
            """
        ),
        {"id": job.id, "fence": job.fence, "ttl": lease_ttl_s},
    )
    return result.scalar_one_or_none() is not None


async def assert_fence(conn: AsyncConnection, job: Job) -> None:
    """Lock the job row and check the caller still owns it.

    Call this first in any transaction that writes on behalf of a job. The row lock is held
    until commit, so the reaper cannot requeue the job halfway through the transaction.
    """
    result = await conn.execute(
        text("SELECT 1 FROM jobs WHERE id = :id AND fence = :fence AND status = 'running' FOR UPDATE"),
        {"id": job.id, "fence": job.fence},
    )
    if result.scalar_one_or_none() is None:
        raise LeaseLost(f"job {job.id} fence {job.fence}")


async def complete(conn: AsyncConnection, job: Job) -> None:
    await _finish(conn, job, "status = 'done', last_error = NULL")


async def fail(
    conn: AsyncConnection,
    job: Job,
    error: str,
    *,
    retryable: bool,
    backoff_base_s: float,
    backoff_max_s: float,
) -> str:
    """Record a failed run. Returns the new status: ``queued`` (will retry) or ``dead``."""
    delay = backoff_delay(job.attempts, base_s=backoff_base_s, max_s=backoff_max_s)
    result = await conn.execute(
        text(
            """
            UPDATE jobs
            SET status = CASE WHEN :retryable AND attempts < max_attempts THEN 'queued' ELSE 'dead' END,
                run_after = now() + make_interval(secs => :delay),
                lease_owner = NULL, lease_until = NULL,
                last_error = :error, updated_at = now()
            WHERE id = :id AND fence = :fence AND status = 'running'
            RETURNING status
            """
        ),
        {"id": job.id, "fence": job.fence, "retryable": retryable, "delay": delay, "error": error[:2000]},
    )
    status = result.scalar_one_or_none()
    if status is None:
        raise LeaseLost(f"job {job.id} fence {job.fence}")
    return str(status)


async def release(conn: AsyncConnection, job: Job) -> None:
    """Hand a job back without counting the run as an attempt (graceful shutdown)."""
    await _finish(conn, job, "status = 'queued', attempts = attempts - 1, run_after = now()")


async def reap(conn: AsyncConnection) -> list[tuple[int, str]]:
    """Requeue jobs whose lease expired; dead-letter those out of attempts.

    Priority and sort_key are left alone, so a reaped job keeps its place in line.
    Safe to run from every worker at once: rows another reaper holds are skipped.
    Returns ``(job_id, new_status)`` for each reaped job.
    """
    result = await conn.execute(
        text(
            """
            WITH expired AS (
                SELECT id FROM jobs
                WHERE status = 'running' AND lease_until < now()
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs AS j
            SET status = CASE WHEN j.attempts >= j.max_attempts THEN 'dead' ELSE 'queued' END,
                lease_owner = NULL, lease_until = NULL,
                last_error = 'lease expired', updated_at = now()
            FROM expired
            WHERE j.id = expired.id
            RETURNING j.id, j.status
            """
        )
    )
    return [(row.id, row.status) for row in result]


def backoff_delay(attempt: int, *, base_s: float, max_s: float) -> float:
    """Exponential backoff with jitter: a random value in [cap/2, cap], cap = base * 2^(attempt-1)."""
    cap = min(max_s, base_s * 2 ** max(attempt - 1, 0))
    return cap / 2 + random.uniform(0, cap / 2)


async def _finish(conn: AsyncConnection, job: Job, assignments: str) -> None:
    result = await conn.execute(
        text(
            f"""
            UPDATE jobs SET {assignments}, lease_owner = NULL, lease_until = NULL, updated_at = now()
            WHERE id = :id AND fence = :fence AND status = 'running'
            RETURNING id
            """
        ),
        {"id": job.id, "fence": job.fence},
    )
    if result.scalar_one_or_none() is None:
        raise LeaseLost(f"job {job.id} fence {job.fence}")


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))
