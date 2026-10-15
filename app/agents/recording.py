"""A finished investigation, out of the database and back in again.

``export`` turns what a live run stored into a recording: the incident rows the agents were
told about, every model step (from llm_calls) and every tool answer (from tool_calls and the
evidence they point at), plus what the run concluded. ``replay`` loads the incident rows into
an empty database and runs the investigation again with the model and the tools answering
from the recording. Same prompts, same code: the same requests, so the same answers, the same
evidence ids and the same diagnosis. Change a prompt or a summary and it stops matching, which
is the point (ADR-0006).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.investigation import AgentConfig, Outcome, investigate
from app.agents.loop import Deps
from app.agents.store import PgAgentStore
from app.db import incidents as incidents_repo
from app.db import investigations
from app.llm.replay import LLMCassette, ReplayBackend
from app.tools import ALL_TOOLS, ToolRegistry
from app.tools.evidence import PgEvidenceStore
from app.tools.sources import Cassette, ReplaySource

# Errors the registry produces itself, before any backend is asked: they replay without a cassette.
# (too_large is not one of them: the registry raises it after the backend answered.)
_LOCAL_ERRORS = ("invalid_args", "unknown_tool")
_INCIDENT_COLUMNS = (
    "id, tenant_id, fingerprint, title, service, environment, priority, state, labels, created_at, closed_at"
)


@dataclass
class Recording:
    incidents: list[dict[str, Any]] = field(default_factory=list)
    annotations: dict[str, dict[str, str]] = field(default_factory=dict)  # incident id -> first alert's
    llm: LLMCassette = field(default_factory=LLMCassette)
    tools: Cassette = field(default_factory=Cassette)
    expected: dict[str, Any] = field(default_factory=dict)  # incident id -> outcome

    def dump(self, out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        doc = {"incidents": self.incidents, "annotations": self.annotations, "expected": self.expected}
        (out / "incidents.json").write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
        self.llm.dump(out / "llm.jsonl.gz")
        self.tools.dump(out / "tools.jsonl.gz")

    @classmethod
    def load(cls, path: Path) -> Recording:
        doc = json.loads((path / "incidents.json").read_text())
        return cls(
            incidents=doc["incidents"],
            annotations=doc["annotations"],
            llm=LLMCassette.load(path / "llm.jsonl.gz"),
            tools=Cassette.load(path / "tools.jsonl.gz"),
            expected=doc["expected"],
        )


async def export(engine: AsyncEngine, incident_ids: list[UUID], meta: dict[str, Any] | None = None) -> Recording:
    rec = Recording(llm=LLMCassette(meta=meta or {}), tools=Cassette(meta=meta or {}))
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE id = ANY(:ids)"), {"ids": incident_ids}
        )
        investigated = [dict(r._mapping) for r in rows]
        # The incidents each one was told about are part of what the agents saw.
        related: dict[UUID, dict[str, Any]] = {}
        for inc in investigated:
            view = await investigations.incident_view(conn, inc)
            if not view.related:
                continue
            more = await conn.execute(
                text(
                    f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE tenant_id = :t AND environment = :e AND id <> :id "
                    "AND created_at BETWEEN :start AND :end AND (closed_at IS NULL OR closed_at > :created)"
                ),
                {
                    "t": inc["tenant_id"],
                    "e": inc["environment"],
                    "id": inc["id"],
                    "start": inc["created_at"] - investigations.RELATED_BEFORE,
                    "end": inc["created_at"] + investigations.RELATED_AFTER,
                    "created": inc["created_at"],
                },
            )
            for r in more:
                related.setdefault(r.id, dict(r._mapping))
        everything = {i["id"]: i for i in [*related.values(), *investigated]}
        rec.incidents = sorted(everything.values(), key=lambda i: (i["created_at"], str(i["id"])))
        for inc_id in everything:
            first = await conn.scalar(
                text(
                    "SELECT annotations FROM alerts WHERE incident_id = :id AND status = 'firing' ORDER BY id LIMIT 1"
                ),
                {"id": inc_id},
            )
            if first:
                rec.annotations[str(inc_id)] = dict(first)

        calls = await conn.execute(
            text(
                "SELECT agent, prompt_version, request_hash, model, step, input_tokens, output_tokens, cache_read_tokens, "
                "cache_creation_tokens, cost_usd, latency_ms, backend FROM llm_calls "
                "WHERE incident_id = ANY(:ids) AND status = 'ok' ORDER BY id"
            ),
            {"ids": incident_ids},
        )
        for c in calls:
            rec.llm.entries.append(
                {
                    "agent": c.agent,
                    "prompt_version": c.prompt_version,
                    "request_hash": c.request_hash,
                    "model": c.model,
                    "step": c.step,
                    "usage": {
                        "input_tokens": c.input_tokens,
                        "output_tokens": c.output_tokens,
                        "cache_read_tokens": c.cache_read_tokens,
                        "cache_creation_tokens": c.cache_creation_tokens,
                    },
                    "cost_usd": float(c.cost_usd) if c.cost_usd is not None else None,
                    "latency_ms": c.latency_ms,
                    "backend": c.backend,
                }
            )

        tool_rows = await conn.execute(
            text(
                """
                SELECT t.incident_id, t.agent, t.tool, t.args, t.status, t.error_code, t.error, t.created_at,
                       e.result, e.fetched_at
                FROM tool_calls t LEFT JOIN evidence e ON e.id = t.evidence_id
                WHERE t.incident_id = ANY(:ids) ORDER BY t.id
                """
            ),
            {"ids": incident_ids},
        )
        rec.tools.entries += [e for t in tool_rows if (e := tool_entry(t)) is not None]

        for inc_id in incident_ids:
            board = await PgAgentStore(engine).entries(inc_id)
            runs = await PgAgentStore(engine).runs(inc_id)
            state = await conn.scalar(text("SELECT state FROM incidents WHERE id = :id"), {"id": inc_id})
            dx = next((e.body for e in reversed(board) if e.kind == "diagnosis"), None)
            rec.expected[str(inc_id)] = {
                "state": state,
                "diagnosis": dx,
                "agents": {name: r.status for name, r in runs.items()},
                "findings": sum(1 for e in board if e.kind == "finding"),
            }
    return rec


def tool_entry(t: Any) -> dict[str, Any] | None:
    """One tool_calls row (joined with its evidence) as a cassette entry; None if it replays without one."""
    # Tagged with the incident: several incidents of one fault ask the same agent the same
    # question minutes apart, and each must get back its own answer (``incident_tools``).
    entry: dict[str, Any] = {"incident": str(t.incident_id), "tool": t.tool, "args": t.args}
    if t.agent:
        entry["agent"] = t.agent
    if t.status == "ok":
        return entry | {"at": t.fetched_at.isoformat(), "raw": t.result}
    if t.error_code in _LOCAL_ERRORS:
        return None
    return entry | {"at": t.created_at.isoformat(), "error": {"code": t.error_code, "message": t.error}}


def incident_tools(rec: Recording, incident_id: UUID) -> Cassette:
    """The tool answers one incident's investigation got (all of them in an untagged recording)."""
    mine = [e for e in rec.tools.entries if e.get("incident", str(incident_id)) == str(incident_id)]
    return Cassette(mine, rec.tools.meta)


async def load_incidents(engine: AsyncEngine, rec: Recording) -> None:
    """Insert the recorded incidents (same ids, same timestamps) and their first alerts."""
    async with engine.begin() as conn:
        for inc in rec.incidents:
            await conn.execute(
                text(
                    """
                    INSERT INTO incidents (id, tenant_id, fingerprint, title, service, environment, priority, state,
                                           labels, created_at, updated_at, closed_at)
                    VALUES (:id, :tenant_id, :fingerprint, :title, :service, :environment, :priority, :state,
                            CAST(:labels AS jsonb), :created_at, :created_at, :closed_at)
                    """
                ),
                {**inc, "labels": json.dumps(inc["labels"])},
            )
            annotations = rec.annotations.get(str(inc["id"]))
            if annotations is not None:
                await conn.execute(
                    text(
                        "INSERT INTO alerts (tenant_id, incident_id, fingerprint, status, labels, annotations) "
                        "VALUES (:t, :id, :fp, 'firing', CAST(:labels AS jsonb), CAST(:ann AS jsonb))"
                    ),
                    {
                        "t": inc["tenant_id"],
                        "id": inc["id"],
                        "fp": inc["fingerprint"],
                        "labels": json.dumps(inc["labels"]),
                        "ann": json.dumps(annotations),
                    },
                )


async def replay(engine: AsyncEngine, rec: Recording, incident_id: UUID, config: AgentConfig) -> Outcome:
    """Run one recorded incident's investigation again, answering from the recording."""
    async with engine.connect() as conn:
        row = await incidents_repo.get(conn, incident_id, tenant_id=None)
        assert row is not None, f"incident {incident_id} is not loaded"
        view = await investigations.incident_view(conn, row)
    deps = Deps(
        backend=ReplayBackend(rec.llm),
        registry=ToolRegistry(ALL_TOOLS, ReplaySource(incident_tools(rec, incident_id)), PgEvidenceStore(engine)),
        store=PgAgentStore(engine),
        tenant_id=view.tenant_id,
        incident_id=view.id,
        token_budget=10**9,
    )
    return await investigate(view, deps, config)
