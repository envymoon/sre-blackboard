"""Hash-chained audit log.

Each row stores ``hash = sha256(prev_hash + canonical row content)``. Changing, deleting or
reordering any row breaks every hash after it, and ``verify`` reports the first bad row.
Appends are serialized with a transaction-scoped advisory lock, so the chain has no forks
even when many transactions write audit events at once.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

GENESIS = "0" * 64
# Arbitrary constant naming the advisory lock that serializes appends.
_LOCK_KEY = 0x5E_A0D17


async def append(
    conn: AsyncConnection,
    *,
    actor: str,
    event: str,
    subject: str,
    tenant_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Append one event inside the caller's transaction. Returns the new row's hash."""
    await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = (await conn.execute(text("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1"))).scalar()
    prev_hash = prev or GENESIS
    ts = datetime.now(UTC)
    body = payload or {}
    digest = _digest(prev_hash, ts, tenant_id, actor, event, subject, body)
    await conn.execute(
        text(
            """
            INSERT INTO audit_log (ts, tenant_id, actor, event, subject, payload, prev_hash, hash)
            VALUES (:ts, :tenant_id, :actor, :event, :subject, CAST(:payload AS jsonb), :prev_hash, :hash)
            """
        ),
        {
            "ts": ts,
            "tenant_id": tenant_id,
            "actor": actor,
            "event": event,
            "subject": subject,
            "payload": json.dumps(body),
            "prev_hash": prev_hash,
            "hash": digest,
        },
    )
    return digest


@dataclass(frozen=True)
class Verification:
    ok: bool
    checked: int
    first_bad_seq: int | None = None
    reason: str = ""


async def verify(conn: AsyncConnection) -> Verification:
    """Recompute the whole chain in ``seq`` order."""
    prev_hash = GENESIS
    checked = 0
    result = await conn.stream(
        text("SELECT seq, ts, tenant_id, actor, event, subject, payload, prev_hash, hash FROM audit_log ORDER BY seq")
    )
    async for row in result:
        if row.prev_hash != prev_hash:
            return Verification(False, checked, row.seq, "prev_hash does not match the previous row")
        expected = _digest(prev_hash, row.ts, row.tenant_id, row.actor, row.event, row.subject, row.payload)
        if row.hash != expected:
            return Verification(False, checked, row.seq, "row content does not match its hash")
        prev_hash = row.hash
        checked += 1
    return Verification(True, checked)


def _digest(
    prev_hash: str,
    ts: datetime,
    tenant_id: str | None,
    actor: str,
    event: str,
    subject: str,
    payload: dict[str, Any],
) -> str:
    canonical = json.dumps(
        [prev_hash, ts.astimezone(UTC).isoformat(), tenant_id, actor, event, subject, payload],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
