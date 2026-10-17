"""M4 acceptance: while a fault is active, the agents must diagnose the incidents it opened.

For each fault:

1. Wait until every incident opened since the injection has finished its investigation
   (DIAGNOSED or ESCALATED), up to ``timeout_s``.
2. The fault's answer is the diagnosis of the first incident it opened (ties: higher
   priority, then service name); it is correct when its top root cause is the service the
   fault was injected into. The other incidents are reported too.
3. Export each fault's investigations as a recording (app/agents/recording.py) for the
   replay tests.
4. After recovery, close the fault's incidents (ESCALATED, "lab: fault recovered"), so the
   next fault opens new ones instead of merging into these. Closing an incident after
   remediation is M5's job; until then the lab does it.

Acceptance (plan, M4): every fault gets a root cause candidate, and the top candidate is
right for at least 2 of the 3.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents import recording
from app.db import incidents
from scripts.lab.faults import Fault

# Still being investigated; with M5, also still being planned and reviewed (DIAGNOSED is a
# stop on the way to a reviewed plan, unless remediation is off).
INVESTIGATING = ("RECEIVED", "TRIAGED", "INVESTIGATING")
PLANNING = ("DIAGNOSED", "PLANNED", "SAFETY_REVIEWED")


@dataclass
class DiagnosisSeen:
    incident_id: str
    title: str
    service: str
    priority: int
    state: str
    seconds_after_injection: float
    root_causes: list[dict[str, Any]] = field(default_factory=list)  # service, confidence, cause
    reason: str = ""  # the state transition's reason (escalation reason, or the top candidate)
    investigation_s: float | None = None  # incident opened -> investigation finished
    agents: dict[str, str] = field(default_factory=dict)
    calls: int = 0
    tokens: int = 0
    cost_usd: float | None = None

    @property
    def top(self) -> str | None:
        return self.root_causes[0]["service"] if self.root_causes else None


def primary(diagnoses: list[DiagnosisSeen]) -> DiagnosisSeen | None:
    """The first incident the fault opened."""
    return min(diagnoses, key=lambda d: (d.seconds_after_injection, d.priority, d.service), default=None)


class Diagnoser:
    def __init__(
        self,
        engine: Engine,
        database_url: str,
        tenant: str,
        out: Path | None,
        timeout_s: float = 900,
        planning: bool = True,
    ):
        self.engine = engine
        self.database_url = database_url
        self.tenant = tenant
        self.out = out
        self.timeout_s = timeout_s
        # Wait for the plan and its review too (M5), not only the diagnosis.
        self.pending = INVESTIGATING + PLANNING if planning else INVESTIGATING

    def _incidents(self, since: datetime) -> list[Any]:
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT id, title, service, priority, state, created_at FROM incidents "
                        "WHERE tenant_id = :t AND created_at >= :since ORDER BY created_at, priority, service"
                    ),
                    {"t": self.tenant, "since": since},
                )
            )

    def wait(self, fault: Fault, t0: datetime, log: Any) -> list[DiagnosisSeen]:
        deadline = time.monotonic() + self.timeout_s
        while True:
            rows = self._incidents(t0)
            pending = [r for r in rows if r.state in self.pending]
            if rows and not pending:
                break
            if time.monotonic() > deadline:
                log(
                    f"[{fault.name}] investigations not finished after {self.timeout_s:.0f}s: "
                    f"{', '.join(f'{r.title}/{r.service} {r.state}' for r in pending) or 'no incident'}"
                )
                break
            time.sleep(10)
        seen = [self._read(r, t0) for r in rows]
        for d in seen:
            causes = ", ".join(f"{c['service']} {c['confidence']:.0%}" for c in d.root_causes) or "none"
            log(
                f"[{fault.name}] {d.state.lower()} {d.title}/{d.service}: root causes {causes}; "
                f"{d.calls} model calls, {d.tokens:,} tokens"
                + (f", ${d.cost_usd:.3f}" if d.cost_usd is not None else "")
                + (f" ({d.reason})" if d.state == "ESCALATED" else "")
            )
        if self.out is not None and rows:
            try:
                rec = asyncio.run(
                    self._export([r.id for r in rows], {"fault": fault.name, "root_cause": fault.root_cause})
                )
                rec.dump(self.out / fault.name)
                log(
                    f"[{fault.name}] recorded {len(rec.llm.entries)} model steps, {len(rec.tools.entries)} tool answers"
                )
            except Exception as exc:  # a recording problem must not fail the acceptance itself
                log(f"[{fault.name}] recording failed: {type(exc).__name__}: {exc}")
        return seen

    def _read(self, row: Any, t0: datetime) -> DiagnosisSeen:
        with self.engine.connect() as conn:
            body = conn.scalar(
                text(
                    "SELECT body FROM blackboard WHERE incident_id = :id AND kind = 'diagnosis' ORDER BY id DESC LIMIT 1"
                ),
                {"id": row.id},
            )
            # How the investigation ended (DIAGNOSED or ESCALATED), whatever happened after.
            last = conn.execute(
                text(
                    "SELECT payload->>'reason' AS reason, ts FROM audit_log WHERE subject = :s "
                    "AND event = 'incident.transition' AND payload->>'from' = 'INVESTIGATING' ORDER BY seq LIMIT 1"
                ),
                {"s": str(row.id)},
            ).one_or_none()
            agents: dict[str, str] = {
                r.agent: r.status
                for r in conn.execute(
                    text("SELECT agent, status FROM agent_runs WHERE incident_id = :id"), {"id": row.id}
                )
            }
            usage = conn.execute(
                text(
                    "SELECT count(*) AS calls, COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens "
                    "+ cache_creation_tokens), 0) AS tokens, SUM(cost_usd) AS cost FROM llm_calls WHERE incident_id = :id"
                ),
                {"id": row.id},
            ).one()
        causes = [
            {"service": c["service"], "confidence": c["confidence"], "cause": c["cause"]}
            for c in ((body or {}).get("root_causes") or [])
        ]
        return DiagnosisSeen(
            incident_id=str(row.id),
            title=row.title,
            service=row.service,
            priority=row.priority,
            state=row.state,
            seconds_after_injection=round((row.created_at - t0).total_seconds(), 1),
            root_causes=causes,
            reason=(last.reason if last else "") or "",
            investigation_s=round((last.ts - row.created_at).total_seconds(), 1) if last is not None else None,
            agents=agents,
            calls=usage.calls,
            tokens=int(usage.tokens),
            cost_usd=float(usage.cost) if usage.cost is not None else None,
        )

    async def _export(self, ids: list[UUID], meta: dict[str, Any]) -> recording.Recording:
        engine = create_async_engine(self.database_url)
        try:
            return await recording.export(engine, ids, meta)
        finally:
            await engine.dispose()

    def close(self, t0: datetime) -> int:
        return asyncio.run(self._close(t0))

    async def _close(self, t0: datetime) -> int:
        engine = create_async_engine(self.database_url)
        closed = 0
        try:
            async with engine.begin() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT id, state, version FROM incidents WHERE tenant_id = :t AND created_at >= :since "
                        "AND closed_at IS NULL"
                    ),
                    {"t": self.tenant, "since": t0},
                )
                for r in rows.all():
                    await incidents.transition(
                        conn,
                        r.id,
                        from_state=r.state,
                        to_state="ESCALATED",
                        expected_version=r.version,
                        actor="lab:accept",
                        reason="lab: fault recovered",
                    )
                    closed += 1
        finally:
            await engine.dispose()
        return closed
