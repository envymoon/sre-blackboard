"""The console's change stream: Server-Sent Events, so a page updates the part that changed.

Each event says what changed, not the whole new state: an audit row (a state move, a plan, a
decision, an execution, a check), or "these agents of this incident took a step". The page
then refetches only that piece. The stream reads the same tables as everything else, from
the caller's tenant only, so any API process can serve any client.

Event ids are a cursor (audit seq, last model step id, last tool call id). A client that
reconnects sends the last one back as Last-Event-ID and misses nothing; one without an id
starts from now. A connection ends after ``stream_max_s`` and the client reconnects, which
keeps idle proxies from cutting it at a random moment.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.security import Principal, require

router = APIRouter(prefix="/api/v1", tags=["stream"])

_BATCH = 500
_PING_S = 15.0


@dataclass(frozen=True)
class Cursor:
    audit: int = 0
    llm: int = 0
    tool: int = 0

    def __str__(self) -> str:
        return f"{self.audit}.{self.llm}.{self.tool}"

    @classmethod
    def parse(cls, value: str | None) -> Cursor | None:
        try:
            a, m, t = (int(p) for p in (value or "").split("."))
        except ValueError:
            return None
        return cls(a, m, t) if min(a, m, t) >= 0 else None


async def now(engine: AsyncEngine, tenant_id: str) -> Cursor:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT (SELECT COALESCE(MAX(seq), 0) FROM audit_log WHERE tenant_id = :t) AS audit,
                           (SELECT COALESCE(MAX(id), 0) FROM llm_calls WHERE tenant_id = :t) AS llm,
                           (SELECT COALESCE(MAX(id), 0) FROM tool_calls WHERE tenant_id = :t) AS tool
                    """
                ),
                {"t": tenant_id},
            )
        ).one()
    return Cursor(row.audit, row.llm, row.tool)


Change = tuple[str, dict[str, Any], Cursor]


async def changes(engine: AsyncEngine, tenant_id: str, since: Cursor) -> tuple[list[Change], Cursor]:
    """What happened after ``since``, oldest first, and the new cursor.

    Each change carries the cursor to resume from once it is delivered, so a connection cut
    halfway through a batch neither loses nor repeats audit rows.
    """
    out: list[Change] = []
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT seq, ts, actor, event, subject, payload FROM audit_log "
                    "WHERE tenant_id = :t AND seq > :a ORDER BY seq LIMIT :n"
                ),
                {"t": tenant_id, "a": since.audit, "n": _BATCH},
            )
        ).all()
        audit = since.audit
        for r in rows:
            audit = r.seq
            out.append(("audit", {**r._mapping, "ts": r.ts.isoformat()}, Cursor(audit, since.llm, since.tool)))
        activity: dict[str, set[str]] = {}
        last = {"llm": since.llm, "tool": since.tool}
        for kind, table in (("llm", "llm_calls"), ("tool", "tool_calls")):
            # Bounded like the audit rows: a backlog drains over several polls, never in one.
            found = (
                await conn.execute(
                    text(
                        f"SELECT id, incident_id, agent FROM {table} "
                        "WHERE tenant_id = :t AND id > :since ORDER BY id LIMIT :n"
                    ),
                    {"t": tenant_id, "since": last[kind], "n": _BATCH},
                )
            ).all()
            for r in found:
                last[kind] = r.id
                if r.incident_id is not None:
                    activity.setdefault(str(r.incident_id), set()).add(r.agent or "")
    after = Cursor(audit, last["llm"], last["tool"])
    out += [("activity", {"incident_id": i, "agents": sorted(a - {""})}, after) for i, a in sorted(activity.items())]
    return out, after


def _frame(event: str, data: dict[str, Any], cursor: Cursor) -> str:
    return f"id: {cursor}\nevent: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"


@router.get("/stream")
async def stream(
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    engine: AsyncEngine = request.app.state.engine
    settings = request.app.state.settings
    start = Cursor.parse(last_event_id) or await now(engine, principal.tenant_id)

    async def events() -> AsyncIterator[str]:
        cursor = start
        yield f"retry: 2000\n{_frame('ready', {}, cursor)}"
        began = pinged = time.monotonic()
        while time.monotonic() - began < settings.stream_max_s and not await request.is_disconnected():
            found, cursor = await changes(engine, principal.tenant_id, cursor)
            for event, data, at in found:
                yield _frame(event, data, at)
            if found:
                pinged = time.monotonic()
            elif time.monotonic() - pinged >= _PING_S:
                yield ": ping\n\n"
                pinged = time.monotonic()
            await asyncio.sleep(settings.stream_poll_s)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
