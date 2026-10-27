"""Reading an incident's investigation: what the agents are told, and what they produced."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agents.investigation import IncidentView
from app.ingest import PRIORITY_NAMES

# Incidents already open when this one opened are probably one problem seen from several
# services. Only those: one opened later may or may not exist yet when the investigation
# starts (each alert group is its own webhook), and what the agents are told must not depend
# on that race, or a replay is told something else. The later one is told about this one.
RELATED_BEFORE = timedelta(minutes=30)


async def incident_view(conn: AsyncConnection, incident: Mapping[Any, Any]) -> IncidentView:
    """Only stored rows go in, never the clock: a replayed investigation renders the same text."""
    annotations = await conn.scalar(
        # The first notification's: later ones may carry newer values, and the text must not drift.
        text("SELECT annotations FROM alerts WHERE incident_id = :id AND status = 'firing' ORDER BY id LIMIT 1"),
        {"id": incident["id"]},
    )
    related = await conn.execute(
        text(
            """
            SELECT title, service, priority, created_at FROM incidents
            WHERE tenant_id = :tenant AND environment = :env AND id <> :id
              AND created_at BETWEEN :start AND :created
              AND (closed_at IS NULL OR closed_at > :created)
            ORDER BY created_at, service, title
            """
        ),
        {
            "tenant": incident["tenant_id"],
            "env": incident["environment"],
            "id": incident["id"],
            "start": incident["created_at"] - RELATED_BEFORE,
            "created": incident["created_at"],
        },
    )
    return IncidentView(
        id=incident["id"],
        tenant_id=incident["tenant_id"],
        title=incident["title"],
        service=incident["service"],
        environment=incident["environment"],
        priority=PRIORITY_NAMES[incident["priority"]],
        created_at=incident["created_at"],
        labels=dict(incident["labels"]),
        annotations=dict(annotations or {}),
        related=[
            {"title": r.title, "service": r.service, "priority": PRIORITY_NAMES[r.priority], "created_at": r.created_at}
            for r in related
        ],
    )


async def usage(conn: AsyncConnection, incident_id: UUID) -> dict[str, Any]:
    row = (
        await conn.execute(
            text(
                """
                SELECT count(*) AS calls,
                       count(*) FILTER (WHERE status = 'error') AS errors,
                       COALESCE(SUM(input_tokens + cache_read_tokens + cache_creation_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       SUM(cost_usd) AS cost_usd,
                       COALESCE(SUM(latency_ms), 0) AS latency_ms
                FROM llm_calls WHERE incident_id = :id
                """
            ),
            {"id": incident_id},
        )
    ).one()
    return {
        "calls": row.calls,
        "errors": row.errors,
        "prompt_tokens": int(row.prompt_tokens),
        "output_tokens": int(row.output_tokens),
        "tokens": int(row.prompt_tokens) + int(row.output_tokens),
        "cost_usd": float(row.cost_usd) if row.cost_usd is not None else None,
        "model_time_ms": int(row.latency_ms),
    }


async def evidence_index(conn: AsyncConnection, incident_id: UUID, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Evidence rows without the (large) raw result."""
    if not ids:
        return {}
    rows = await conn.execute(
        text(
            "SELECT id, tool, args, result_hash, result_bytes, fetched_at, source FROM evidence "
            "WHERE incident_id = :inc AND id = ANY(:ids)"
        ),
        {"inc": incident_id, "ids": ids},
    )
    return {r.id: dict(r._mapping) for r in rows}


async def agent_tree(conn: AsyncConnection, incident_id: UUID) -> list[dict[str, Any]]:
    """Every agent run with its model steps, and under each step the tool calls it asked for.

    A step's row is written when the model answers, before its tools run, and the next step's
    after they all came back: so a tool call belongs to the agent's last step written before it.
    """
    runs = await conn.execute(
        text(
            "SELECT agent, status, model, prompt_version, steps, error, started_at, finished_at "
            "FROM agent_runs WHERE incident_id = :id ORDER BY started_at, id"
        ),
        {"id": incident_id},
    )
    calls = await conn.execute(
        text(
            """
            SELECT agent, step_no, status, error_code, error, attempts, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens, cost_usd, latency_ms, created_at,
                   step->>'reasoning' AS reasoning, COALESCE(jsonb_typeof(step->'final'), 'null') <> 'null' AS final
            FROM llm_calls WHERE incident_id = :id ORDER BY created_at, id
            """
        ),
        {"id": incident_id},
    )
    tools = await conn.execute(
        text(
            "SELECT agent, tool, args, status, error_code, attempts, latency_ms, evidence_id, created_at "
            "FROM tool_calls WHERE incident_id = :id ORDER BY created_at, id"
        ),
        {"id": incident_id},
    )
    steps: dict[str, list[dict[str, Any]]] = {}
    for c in calls:
        step = dict(c._mapping)
        agent = step.pop("agent")
        step["cost_usd"] = float(step["cost_usd"]) if step["cost_usd"] is not None else None
        step["tools"] = []
        steps.setdefault(agent, []).append(step)
    for t in tools:
        call = dict(t._mapping)
        mine = steps.get(call.pop("agent") or "", [])
        owner = next((s for s in reversed(mine) if s["created_at"] <= call["created_at"]), None)
        if owner is not None:
            owner["tools"].append(call)
    out = []
    for r in runs:
        mine = steps.get(r.agent, [])
        out.append(
            {
                **r._mapping,
                "usage": {
                    "calls": len(mine),
                    "prompt_tokens": sum(
                        s["input_tokens"] + s["cache_read_tokens"] + s["cache_creation_tokens"] for s in mine
                    ),
                    "output_tokens": sum(s["output_tokens"] for s in mine),
                    "cost_usd": sum(s["cost_usd"] or 0.0 for s in mine),
                    "model_time_ms": sum(s["latency_ms"] for s in mine),
                    "tool_calls": sum(len(s["tools"]) for s in mine),
                },
                "llm_calls": mine,
            }
        )
    return out
