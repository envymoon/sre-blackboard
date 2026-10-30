"""What is in the database right now, as Prometheus gauges, read when the API is scraped.

Queue depth, the age of the oldest job nobody has claimed, dead letters, open incidents, the
oldest incident still without a diagnosis, the oldest plan waiting for an approval. A worker
cannot report these about itself once it is stuck or gone; the API reads them from Postgres.

Every priority is always reported (0 when there is nothing), so an alert on "the oldest P0
waits longer than 2 minutes" sees a value, not an absent series.
"""

from __future__ import annotations

from collections.abc import Iterator

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.obs.tracing import PRIORITIES

UNDIAGNOSED = ("RECEIVED", "TRIAGED", "INVESTIGATING")


class _Static(Collector):
    def __init__(self, families: list[GaugeMetricFamily]) -> None:
        self.families = families

    def collect(self) -> Iterator[Metric]:
        return iter(self.families)


async def read(conn: AsyncConnection) -> list[GaugeMetricFamily]:
    jobs = GaugeMetricFamily(
        "sre_queue_jobs", "Jobs not done, by kind, status and priority", labels=["kind", "status", "priority"]
    )
    for r in await conn.execute(
        text("SELECT kind, status, priority, count(*) AS n FROM jobs WHERE status <> 'done' GROUP BY 1, 2, 3")
    ):
        jobs.add_metric([r.kind, r.status, PRIORITIES[r.priority]], r.n)

    ready = GaugeMetricFamily(
        "sre_queue_ready_oldest_seconds",
        "How long the oldest runnable, unclaimed job has been waiting, by priority",
        labels=["priority"],
    )
    rows = await conn.execute(
        text(
            "SELECT priority, EXTRACT(EPOCH FROM now() - min(run_after)) AS age FROM jobs "
            "WHERE status = 'queued' AND run_after <= now() GROUP BY 1"
        )
    )
    _per_priority(ready, {r.priority: float(r.age) for r in rows})

    open_ = GaugeMetricFamily(
        "sre_incidents_open", "Open incidents, by state and priority", labels=["state", "priority"]
    )
    for r in await conn.execute(
        text("SELECT state, priority, count(*) AS n FROM incidents WHERE closed_at IS NULL GROUP BY 1, 2")
    ):
        open_.add_metric([r.state, PRIORITIES[r.priority]], r.n)

    undiagnosed = GaugeMetricFamily(
        "sre_incidents_undiagnosed_oldest_seconds",
        "Age of the oldest open incident without a diagnosis yet, by priority",
        labels=["priority"],
    )
    rows = await conn.execute(
        text(
            "SELECT priority, EXTRACT(EPOCH FROM now() - min(created_at)) AS age FROM incidents "
            "WHERE closed_at IS NULL AND state = ANY(:states) GROUP BY 1"
        ),
        {"states": list(UNDIAGNOSED)},
    )
    _per_priority(undiagnosed, {r.priority: float(r.age) for r in rows})

    approval = GaugeMetricFamily(
        "sre_approvals_pending_oldest_seconds", "How long the oldest plan has been waiting for an approval"
    )
    age = await conn.scalar(
        text("SELECT EXTRACT(EPOCH FROM now() - min(updated_at)) FROM incidents WHERE state = 'AWAITING_APPROVAL'")
    )
    approval.add_metric([], float(age or 0))
    return [jobs, ready, open_, undiagnosed, approval]


def _per_priority(family: GaugeMetricFamily, values: dict[int, float]) -> None:
    for i, name in enumerate(PRIORITIES):
        family.add_metric([name], round(values.get(i, 0.0), 3))


async def render(conn: AsyncConnection | None) -> bytes:
    """The gauges in the text exposition format, plus whether reading them worked."""
    up = GaugeMetricFamily("sre_state_up", "1 if the API could read the queue and incident state from Postgres")
    families: list[GaugeMetricFamily] = []
    try:
        if conn is None:
            raise RuntimeError("no database")
        families = await read(conn)
        up.add_metric([], 1)
    except Exception:
        up.add_metric([], 0)
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_Static([*families, up]))
    return generate_latest(registry)
