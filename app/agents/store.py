"""Where an investigation keeps its state: LLM call ledger, agent run checkpoints, blackboard.

``PgAgentStore`` writes checkpoints and blackboard rows through a fenced transaction when one
is given (the worker passes ``JobContext.transaction``), so a worker that lost its job can
still finish talking to the model but can no longer record anything as done.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.llm.types import Usage
from app.tools.evidence import EvidenceRecord, ToolCallRecord


@dataclass(frozen=True)
class LLMCallRecord:
    tenant_id: str
    incident_id: UUID | None
    agent: str
    step_no: int
    backend: str
    model: str
    prompt_version: str
    request_hash: str
    status: str  # ok | error
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None
    latency_ms: int = 0
    attempts: int = 1
    step: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Entry:
    kind: str  # plan | finding | diagnosis
    author: str
    body: dict[str, Any]
    evidence_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunRecord:
    agent: str
    status: str
    model: str
    prompt_version: str
    steps: int
    result: dict[str, Any]


class AgentStore(Protocol):
    async def record_llm_call(self, rec: LLMCallRecord) -> None: ...

    async def tokens_used(self, incident_id: UUID) -> int: ...

    async def evidence_tools(self, incident_id: UUID, ids: Collection[str]) -> dict[str, str]: ...

    async def runs(self, incident_id: UUID) -> dict[str, RunRecord]: ...

    async def start_run(
        self, tenant_id: str, incident_id: UUID, agent: str, model: str, prompt_version: str
    ) -> None: ...

    async def finish_run(
        self, tenant_id: str, incident_id: UUID, agent: str, steps: int, result: dict[str, Any], entries: list[Entry]
    ) -> None: ...

    async def fail_run(self, incident_id: UUID, agent: str, steps: int, error: str) -> None: ...

    async def entries(self, incident_id: UUID, kind: str | None = None) -> list[Entry]: ...


@dataclass
class MemoryAgentStore:
    """For unit tests and the CLI. Also the tool registry's evidence sink, so it knows which
    evidence belongs to which incident (``evidence``: id -> (incident id, tool))."""

    evidence: dict[str, tuple[UUID | None, str]] = field(default_factory=dict)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    calls: list[LLMCallRecord] = field(default_factory=list)
    run_rows: dict[tuple[UUID, str], RunRecord] = field(default_factory=dict)
    board: dict[UUID, list[Entry]] = field(default_factory=dict)

    async def save(self, evidence: EvidenceRecord | None, call: ToolCallRecord) -> None:
        if evidence is not None:
            self.evidence.setdefault(evidence.id, (evidence.incident_id, evidence.tool))
        self.tool_calls.append(call)

    async def record_llm_call(self, rec: LLMCallRecord) -> None:
        self.calls.append(rec)

    async def tokens_used(self, incident_id: UUID) -> int:
        return sum(c.usage.total for c in self.calls if c.incident_id == incident_id)

    async def evidence_tools(self, incident_id: UUID, ids: Collection[str]) -> dict[str, str]:
        return {i: self.evidence[i][1] for i in ids if i in self.evidence and self.evidence[i][0] == incident_id}

    async def runs(self, incident_id: UUID) -> dict[str, RunRecord]:
        return {agent: r for (inc, agent), r in self.run_rows.items() if inc == incident_id}

    async def start_run(self, tenant_id: str, incident_id: UUID, agent: str, model: str, prompt_version: str) -> None:
        current = self.run_rows.get((incident_id, agent))
        if current is None or current.status != "done":
            self.run_rows[(incident_id, agent)] = RunRecord(agent, "running", model, prompt_version, 0, {})

    async def finish_run(
        self, tenant_id: str, incident_id: UUID, agent: str, steps: int, result: dict[str, Any], entries: list[Entry]
    ) -> None:
        run = self.run_rows[(incident_id, agent)]
        self.run_rows[(incident_id, agent)] = RunRecord(agent, "done", run.model, run.prompt_version, steps, result)
        self.board.setdefault(incident_id, []).extend(entries)

    async def fail_run(self, incident_id: UUID, agent: str, steps: int, error: str) -> None:
        run = self.run_rows[(incident_id, agent)]
        self.run_rows[(incident_id, agent)] = RunRecord(
            agent, "failed", run.model, run.prompt_version, steps, {"error": error}
        )

    async def entries(self, incident_id: UUID, kind: str | None = None) -> list[Entry]:
        return [e for e in self.board.get(incident_id, []) if kind is None or e.kind == kind]


Fenced = Callable[[], AbstractAsyncContextManager[AsyncConnection]]


class PgAgentStore:
    def __init__(self, engine: AsyncEngine, fenced: Fenced | None = None) -> None:
        self.engine = engine
        self._fenced = fenced

    @contextlib.asynccontextmanager
    async def _write(self) -> AsyncIterator[AsyncConnection]:
        if self._fenced is not None:
            async with self._fenced() as conn:
                yield conn
        else:
            async with self.engine.begin() as conn:
                yield conn

    async def record_llm_call(self, rec: LLMCallRecord) -> None:
        # Not fenced: the tokens were spent whether or not this worker still owns the job.
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO llm_calls (tenant_id, incident_id, agent, step_no, backend, model, prompt_version,
                        request_hash, status, error_code, error, attempts, input_tokens, output_tokens,
                        cache_read_tokens, cache_creation_tokens, cost_usd, latency_ms, step)
                    VALUES (:tenant_id, :incident_id, :agent, :step_no, :backend, :model, :prompt_version,
                        :request_hash, :status, :error_code, :error, :attempts, :input_tokens, :output_tokens,
                        :cache_read_tokens, :cache_creation_tokens, :cost_usd, :latency_ms, CAST(:step AS jsonb))
                    """
                ),
                {
                    "tenant_id": rec.tenant_id,
                    "incident_id": rec.incident_id,
                    "agent": rec.agent,
                    "step_no": rec.step_no,
                    "backend": rec.backend,
                    "model": rec.model,
                    "prompt_version": rec.prompt_version,
                    "request_hash": rec.request_hash,
                    "status": rec.status,
                    "error_code": rec.error_code,
                    "error": rec.error,
                    "attempts": rec.attempts,
                    **rec.usage.to_dict(),
                    "cost_usd": Decimal(str(rec.cost_usd)) if rec.cost_usd is not None else None,
                    "latency_ms": rec.latency_ms,
                    "step": json.dumps(rec.step) if rec.step is not None else None,
                },
            )

    async def tokens_used(self, incident_id: UUID) -> int:
        async with self.engine.connect() as conn:
            used = await conn.scalar(
                text(
                    "SELECT COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0) "
                    "FROM llm_calls WHERE incident_id = :id"
                ),
                {"id": incident_id},
            )
        return int(used or 0)

    async def evidence_tools(self, incident_id: UUID, ids: Collection[str]) -> dict[str, str]:
        if not ids:
            return {}
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT id, tool FROM evidence WHERE incident_id = :inc AND id = ANY(:ids)"),
                {"inc": incident_id, "ids": list(ids)},
            )
            return {r.id: r.tool for r in rows}

    async def runs(self, incident_id: UUID) -> dict[str, RunRecord]:
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT agent, status, model, prompt_version, steps, result FROM agent_runs "
                    "WHERE incident_id = :id ORDER BY id"
                ),
                {"id": incident_id},
            )
            return {
                r.agent: RunRecord(r.agent, r.status, r.model, r.prompt_version, r.steps, dict(r.result)) for r in rows
            }

    async def start_run(self, tenant_id: str, incident_id: UUID, agent: str, model: str, prompt_version: str) -> None:
        async with self._write() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO agent_runs (tenant_id, incident_id, agent, status, model, prompt_version)
                    VALUES (:tenant_id, :incident_id, :agent, 'running', :model, :pv)
                    ON CONFLICT (incident_id, agent) DO UPDATE
                    SET status = 'running', model = :model, prompt_version = :pv, steps = 0, error = NULL,
                        result = '{}'::jsonb, started_at = now(), finished_at = NULL
                    WHERE agent_runs.status <> 'done'
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "incident_id": incident_id,
                    "agent": agent,
                    "model": model,
                    "pv": prompt_version,
                },
            )

    async def finish_run(
        self, tenant_id: str, incident_id: UUID, agent: str, steps: int, result: dict[str, Any], entries: list[Entry]
    ) -> None:
        async with self._write() as conn:
            done = await conn.execute(
                text(
                    """
                    UPDATE agent_runs SET status = 'done', steps = :steps, result = CAST(:result AS jsonb),
                        finished_at = now()
                    WHERE incident_id = :incident_id AND agent = :agent AND status <> 'done'
                    """
                ),
                {"incident_id": incident_id, "agent": agent, "steps": steps, "result": json.dumps(result)},
            )
            if done.rowcount == 0:
                return  # another attempt finished it first; its entries are already there
            for e in entries:
                await conn.execute(
                    text(
                        """
                        INSERT INTO blackboard (tenant_id, incident_id, kind, author, body, evidence_ids)
                        VALUES (:tenant_id, :incident_id, :kind, :author, CAST(:body AS jsonb), :ids)
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "incident_id": incident_id,
                        "kind": e.kind,
                        "author": e.author,
                        "body": json.dumps(e.body),
                        "ids": e.evidence_ids,
                    },
                )

    async def fail_run(self, incident_id: UUID, agent: str, steps: int, error: str) -> None:
        async with self._write() as conn:
            await conn.execute(
                text(
                    "UPDATE agent_runs SET status = 'failed', steps = :steps, error = :error, finished_at = now() "
                    "WHERE incident_id = :incident_id AND agent = :agent AND status <> 'done'"
                ),
                {"incident_id": incident_id, "agent": agent, "steps": steps, "error": error[:2000]},
            )

    async def entries(self, incident_id: UUID, kind: str | None = None) -> list[Entry]:
        query = "SELECT kind, author, body, evidence_ids FROM blackboard WHERE incident_id = :id"
        params: dict[str, Any] = {"id": incident_id}
        if kind is not None:
            query += " AND kind = :kind"
            params["kind"] = kind
        async with self.engine.connect() as conn:
            rows = await conn.execute(text(query + " ORDER BY id"), params)
            return [Entry(r.kind, r.author, dict(r.body), list(r.evidence_ids)) for r in rows]
