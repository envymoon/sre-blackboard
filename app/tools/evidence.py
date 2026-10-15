"""Where tool results go: an immutable evidence row per successful call, a log row per call."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.tools.base import canonical_json, sha256_hex


def evidence_id(scope: str, tool: str, args: dict[str, Any], result_hash: str) -> str:
    """Derived from what the evidence is, not drawn at random: the same answer to the same
    call in the same scope gets the same id. A retried job then reuses its evidence instead of
    duplicating it, and a replayed run shows the model exactly the transcript it saw live."""
    return "ev_" + sha256_hex(canonical_json([scope, tool, args, result_hash]))[:16]


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    tenant_id: str
    incident_id: UUID | None
    tool: str
    args: dict[str, Any]
    result: Any
    result_hash: str
    result_bytes: int
    summary: dict[str, Any]
    truncated: bool
    source: str
    fetched_at: datetime


@dataclass(frozen=True)
class ToolCallRecord:
    tenant_id: str
    incident_id: UUID | None
    agent: str | None
    tool: str
    args: dict[str, Any]
    status: str
    error_code: str | None
    error: str | None
    attempts: int
    latency_ms: int
    evidence_id: str | None


class EvidenceSink(Protocol):
    async def save(self, evidence: EvidenceRecord | None, call: ToolCallRecord) -> None: ...


@dataclass
class MemoryEvidenceStore:
    """For the CLI, recording runs and tests."""

    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)
    calls: list[ToolCallRecord] = field(default_factory=list)

    async def save(self, evidence: EvidenceRecord | None, call: ToolCallRecord) -> None:
        if evidence is not None:
            self.evidence.setdefault(evidence.id, evidence)
        self.calls.append(call)


class PgEvidenceStore:
    """Writes the evidence row and the call row in one transaction."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def save(self, evidence: EvidenceRecord | None, call: ToolCallRecord) -> None:
        async with self.engine.begin() as conn:
            if evidence is not None:
                await conn.execute(
                    text(
                        """
                        INSERT INTO evidence (id, tenant_id, incident_id, tool, args, result, result_hash,
                                              result_bytes, summary, truncated, source, fetched_at)
                        VALUES (:id, :tenant_id, :incident_id, :tool, CAST(:args AS jsonb), CAST(:result AS jsonb),
                                :result_hash, :result_bytes, CAST(:summary AS jsonb), :truncated, :source, :fetched_at)
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": evidence.id,
                        "tenant_id": evidence.tenant_id,
                        "incident_id": evidence.incident_id,
                        "tool": evidence.tool,
                        "args": json.dumps(evidence.args),
                        "result": json.dumps(evidence.result),
                        "result_hash": evidence.result_hash,
                        "result_bytes": evidence.result_bytes,
                        "summary": json.dumps(evidence.summary),
                        "truncated": evidence.truncated,
                        "source": evidence.source,
                        "fetched_at": evidence.fetched_at,
                    },
                )
            await conn.execute(
                text(
                    """
                    INSERT INTO tool_calls (tenant_id, incident_id, agent, tool, args, status, error_code, error,
                                            attempts, latency_ms, evidence_id)
                    VALUES (:tenant_id, :incident_id, :agent, :tool, CAST(:args AS jsonb), :status, :error_code,
                            :error, :attempts, :latency_ms, :evidence_id)
                    """
                ),
                {
                    "tenant_id": call.tenant_id,
                    "incident_id": call.incident_id,
                    "agent": call.agent,
                    "tool": call.tool,
                    "args": json.dumps(call.args),
                    "status": call.status,
                    "error_code": call.error_code,
                    "error": call.error,
                    "attempts": call.attempts,
                    "latency_ms": call.latency_ms,
                    "evidence_id": call.evidence_id,
                },
            )


async def load(engine: AsyncEngine, evidence_id: str, *, tenant_id: str) -> dict[str, Any] | None:
    """One evidence row, scoped to the tenant (another tenant's id reads as missing)."""
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM evidence WHERE id = :id AND tenant_id = :tenant_id"),
                    {"id": evidence_id, "tenant_id": tenant_id},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None
