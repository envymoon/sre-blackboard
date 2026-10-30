"""Worker loop: claim jobs, run handlers, keep leases alive, shut down gracefully.

Delivery is at-least-once: a job can run again after a crash or a lost lease, so every
handler must be idempotent. Writes go through ``JobContext.transaction()``, which checks
the fence first, so a worker that lost its lease cannot change anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import Settings
from app.db import audit
from app.ingest import PRIORITY_NAMES
from app.obs import metrics, tracing
from app.queue import jobs
from app.queue.jobs import Job, LeaseLost

log = logging.getLogger(__name__)


class PermanentJobError(Exception):
    """Raised by a handler when retrying cannot help (bad input, missing incident)."""


@dataclass(frozen=True)
class JobContext:
    job: Job
    engine: AsyncEngine
    settings: Settings
    worker_id: str

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        """A transaction that only commits if this worker still owns the job."""
        async with self.engine.begin() as conn:
            await jobs.assert_fence(conn, self.job)
            yield conn


Handler = Callable[[JobContext], Awaitable[None]]


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"


class Worker:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        handlers: dict[str, Handler],
        *,
        worker_id: str | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.handlers = handlers
        self.worker_id = worker_id or default_worker_id()
        self._running: dict[int, asyncio.Task[None]] = {}
        metrics.WORKER_SLOTS.set(settings.worker_concurrency)

    async def run(self, stop: asyncio.Event) -> None:
        """Process jobs until ``stop`` is set, then drain in-flight jobs."""
        log.info("worker %s started", self.worker_id)
        reaper = asyncio.create_task(self._reap_loop(stop))
        try:
            while not stop.is_set():
                job = await self._claim()
                if job is not None:
                    self._running[job.id] = asyncio.create_task(self._execute(job))
                    continue
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.settings.worker_poll_interval_s)
        finally:
            reaper.cancel()
            await self._drain()
            log.info("worker %s stopped", self.worker_id)

    async def _claim(self) -> Job | None:
        free = self.settings.worker_concurrency - len(self._running)
        if free <= 0:
            return None
        # The last reserved slots are kept for P0, so a P0 never waits for a busy worker.
        max_priority = 3 if free > self.settings.worker_reserved_p0_slots else 0
        try:
            async with self.engine.begin() as conn:
                return await jobs.claim(
                    conn,
                    owner=self.worker_id,
                    lease_ttl_s=self.settings.queue_lease_ttl_s,
                    max_priority=max_priority,
                    kinds=sorted(self.handlers),
                )
        except Exception:
            log.exception("claim failed")
            return None

    async def _execute(self, job: Job) -> None:
        task = asyncio.current_task()
        assert task is not None
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job, task, lease_lost))
        ctx = JobContext(job, self.engine, self.settings, self.worker_id)
        metrics.QUEUE_WAIT.labels(job.kind, PRIORITY_NAMES[job.priority]).observe(job.waited_s)
        metrics.JOBS_RUNNING.inc()
        started = time.monotonic()
        outcome = "error"
        with tracing.job_span(job) as span:
            span.set_attribute("sre.worker", self.worker_id)
            try:
                handler = self.handlers.get(job.kind)
                if handler is None:
                    raise PermanentJobError(f"no handler for job kind {job.kind!r}")
                async with asyncio.timeout(self.settings.worker_job_timeout_s):
                    await handler(ctx)
                async with self.engine.begin() as conn:
                    await jobs.complete(conn, job)
                outcome = "done"
                log.info("job %s (%s) done", job.id, job.kind)
            except LeaseLost:
                outcome = "lease_lost"
                log.warning("job %s: lease lost, result discarded", job.id)
            except asyncio.CancelledError:
                if lease_lost.is_set():
                    outcome = "lease_lost"
                    log.warning("job %s: lease lost, handler cancelled", job.id)
                    return
                outcome = "released"
                await self._release(job)
                raise
            except PermanentJobError as exc:
                outcome = await self._fail(job, exc, retryable=False)
            except Exception as exc:
                outcome = await self._fail(job, exc, retryable=True)
            finally:
                heartbeat.cancel()
                self._running.pop(job.id, None)
                metrics.JOBS_RUNNING.dec()
                metrics.JOBS.labels(job.kind, outcome).inc()
                metrics.observe(metrics.JOB_SECONDS, time.monotonic() - started, kind=job.kind, outcome=outcome)
                span.set_attribute("sre.job.outcome", outcome)
                if outcome not in ("done", "released"):
                    tracing.fail(span, outcome)

    async def _heartbeat(self, job: Job, task: asyncio.Task[None], lease_lost: asyncio.Event) -> None:
        interval = self.settings.queue_lease_ttl_s / 3
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.engine.begin() as conn:
                    alive = await jobs.heartbeat(conn, job, lease_ttl_s=self.settings.queue_lease_ttl_s)
            except Exception:
                # A database blip: keep trying until the lease would have run out anyway.
                log.exception("job %s: heartbeat failed", job.id)
                continue
            if not alive:
                lease_lost.set()
                task.cancel()
                return

    async def _fail(self, job: Job, exc: BaseException, *, retryable: bool) -> str:
        """Record the failure; returns how the attempt ended: retry, dead or lease_lost."""
        error = f"{type(exc).__name__}: {exc}"
        try:
            async with self.engine.begin() as conn:
                status = await jobs.fail(
                    conn,
                    job,
                    error,
                    retryable=retryable,
                    backoff_base_s=self.settings.queue_backoff_base_s,
                    backoff_max_s=self.settings.queue_backoff_max_s,
                )
                if status == "dead":
                    await _audit_dead(conn, job, error, actor=f"worker:{self.worker_id}")
        except LeaseLost:
            log.warning("job %s: lease lost before failure was recorded", job.id)
            return "lease_lost"
        log.warning("job %s (%s) failed, now %s: %s", job.id, job.kind, status, error)
        return "dead" if status == "dead" else "retry"

    async def _release(self, job: Job) -> None:
        try:
            async with self.engine.begin() as conn:
                await jobs.release(conn, job)
            log.info("job %s released for another worker", job.id)
        except LeaseLost:
            pass
        except Exception:
            log.exception("job %s: release failed; the reaper will requeue it", job.id)

    async def _reap_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with self.engine.begin() as conn:
                    for job_id, status in await jobs.reap(conn):
                        log.warning("job %s: lease expired, now %s", job_id, status)
                        metrics.after_commit(conn, metrics.JOBS.labels("any", f"reaped_{status}").inc)
                        if status == "dead":
                            await audit.append(
                                conn,
                                actor=f"worker:{self.worker_id}",
                                event="job.dead",
                                subject=f"job:{job_id}",
                                payload={"error": "lease expired"},
                            )
            except Exception:
                log.exception("reap failed")
            await asyncio.sleep(self.settings.worker_reap_interval_s)

    async def _drain(self) -> None:
        if not self._running:
            return
        tasks = list(self._running.values())
        log.info("waiting up to %ss for %d job(s)", self.settings.worker_shutdown_grace_s, len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=self.settings.worker_shutdown_grace_s)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def _audit_dead(conn: AsyncConnection, job: Job, error: str, *, actor: str) -> None:
    await audit.append(
        conn,
        actor=actor,
        event="job.dead",
        subject=f"job:{job.id}",
        tenant_id=job.tenant_id,
        payload={"kind": job.kind, "attempts": job.attempts, "error": error[:500]},
    )
