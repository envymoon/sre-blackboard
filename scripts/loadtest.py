"""M1 acceptance check: send many duplicate alerts, then verify nothing was duplicated.

Sends ``--alerts`` Alertmanager webhooks spread over ``--distinct`` alert identities, with
``--concurrency`` requests in flight, then waits for the workers to triage every incident
and checks directly in Postgres that:

- exactly ``--distinct`` incidents exist, and their alert counts add up to ``--alerts``;
- there is exactly one triage job per incident, every job is done, and none was claimed
  twice (fence == 1);
- the audit hash chain verifies.

Reads SRE_JWT_SECRET and SRE_DATABASE_URL like the app does. Run against a fresh database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security import mint_token
from app.db import audit
from app.db.engine import make_engine


def payload(i: int, distinct: int) -> dict:
    return {
        "version": "4",
        "status": "firing",
        "receiver": "loadtest",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "service": f"svc-{i % distinct}",
                    "environment": "loadtest",
                    "severity": ["critical", "warning", "info"][i % 3],
                },
                "annotations": {},
            }
        ],
    }


async def send_all(url: str, token: str, n: int, distinct: int, concurrency: int) -> tuple[int, float]:
    sem = asyncio.Semaphore(concurrency)
    failures = 0

    async with httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:

        async def one(i: int) -> None:
            nonlocal failures
            async with sem:
                resp = await client.post("/api/v1/alerts", json=payload(i, distinct))
                if resp.status_code != 202:
                    failures += 1

        start = time.monotonic()
        await asyncio.gather(*(one(i) for i in range(n)))
        return failures, time.monotonic() - start


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--alerts", type=int, default=1000)
    parser.add_argument("--distinct", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--wait", type=float, default=60, help="seconds to wait for triage")
    args = parser.parse_args()

    settings = get_settings()
    token = mint_token(settings, subject="loadtest", tenant_id="loadtest", roles=["ingest"])
    failures, elapsed = await send_all(args.url, token, args.alerts, args.distinct, args.concurrency)
    print(f"sent {args.alerts} alerts in {elapsed:.1f}s ({args.alerts / elapsed:.0f}/s), {failures} failed")

    engine = make_engine(settings)
    try:
        deadline = time.monotonic() + args.wait
        while True:
            async with engine.connect() as conn:
                pending = (
                    await conn.execute(
                        text("SELECT count(*) FROM jobs WHERE tenant_id = 'loadtest' AND status <> 'done'")
                    )
                ).scalar_one()
            if pending == 0 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.5)

        async with engine.connect() as conn:
            inc = (
                await conn.execute(
                    text(
                        "SELECT count(*) AS n, coalesce(sum(alert_count), 0) AS alerts, "
                        "count(*) FILTER (WHERE state = 'TRIAGED') AS triaged "
                        "FROM incidents WHERE tenant_id = 'loadtest'"
                    )
                )
            ).one()
            job = (
                await conn.execute(
                    text(
                        "SELECT count(*) AS n, count(*) FILTER (WHERE status = 'done') AS done, "
                        "coalesce(max(fence), 0) AS max_fence, count(DISTINCT incident_id) AS incidents "
                        "FROM jobs WHERE tenant_id = 'loadtest'"
                    )
                )
            ).one()
            owners = (
                await conn.execute(
                    text("SELECT count(DISTINCT actor) FROM audit_log WHERE event = 'incident.transition'")
                )
            ).scalar_one()
            chain = await audit.verify(conn)
    finally:
        await engine.dispose()

    checks = {
        "no failed requests": failures == 0,
        f"incidents == {args.distinct}": inc.n == args.distinct,
        f"alert counts sum to {args.alerts}": inc.alerts == args.alerts,
        "one triage job per incident": job.n == inc.n == job.incidents,
        "every job done": job.done == job.n,
        "no job claimed twice (max fence 1)": job.max_fence == 1,
        "every incident triaged": inc.triaged == inc.n,
        "audit chain verifies": chain.ok,
    }
    print(f"incidents={inc.n} alerts={inc.alerts} jobs={job.n} done={job.done} max_fence={job.max_fence}")
    print(f"triage was done by {owners} distinct worker(s); audit rows checked: {chain.checked}")
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
