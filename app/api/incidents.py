"""Read-only incident endpoints. Every query is scoped to the caller's tenant."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.engine import RowMapping

from app.agents.report import ReportInput, render_markdown
from app.agents.schemas import Diagnosis, Finding
from app.agents.store import PgAgentStore
from app.core.security import Principal, require
from app.db import incidents, investigations
from app.ingest import PRIORITY_NAMES

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


class IncidentOut(BaseModel):
    id: UUID
    title: str
    service: str
    environment: str
    priority: str
    state: str
    version: int
    alert_count: int
    labels: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None

    @classmethod
    def from_row(cls, row: RowMapping) -> IncidentOut:
        return cls(**{**row, "priority": PRIORITY_NAMES[row["priority"]]})


@router.get("")
async def list_incidents(
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IncidentOut]:
    async with request.app.state.engine.connect() as conn:
        rows = await incidents.list_for_tenant(conn, principal.tenant_id, limit=limit)
    return [IncidentOut.from_row(r) for r in rows]


@router.get("/{incident_id}")
async def get_incident(
    incident_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
) -> IncidentOut:
    async with request.app.state.engine.connect() as conn:
        row = await incidents.get(conn, incident_id, tenant_id=principal.tenant_id)
    if row is None:
        # Another tenant's incident is indistinguishable from a missing one.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    return IncidentOut.from_row(row)


async def _investigation(request: Request, incident_id: UUID, tenant_id: str) -> tuple[RowMapping, ReportInput]:
    engine = request.app.state.engine
    async with engine.connect() as conn:
        row = await incidents.get(conn, incident_id, tenant_id=tenant_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
        store = PgAgentStore(engine)
        board = await store.entries(incident_id)
        runs = await store.runs(incident_id)
        dx = next((e for e in reversed(board) if e.kind == "diagnosis"), None)
        findings = [(e.author, Finding.model_validate(e.body)) for e in board if e.kind == "finding"]
        cited = sorted({ev for e in board for ev in e.evidence_ids})
        data = ReportInput(
            incident={**row, "priority": PRIORITY_NAMES[row["priority"]]},
            diagnosis=Diagnosis.model_validate(dx.body) if dx else None,
            findings=findings,
            evidence=await investigations.evidence_index(conn, incident_id, cited),
            runs=runs,
            usage=await investigations.usage(conn, incident_id),
        )
    return row, data


@router.get("/{incident_id}/diagnosis")
async def get_diagnosis(
    incident_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
) -> dict[str, Any]:
    """The structured result: diagnosis, findings by agent, agent runs and model usage."""
    row, data = await _investigation(request, incident_id, principal.tenant_id)
    return {
        "incident_id": str(incident_id),
        "state": row["state"],
        "diagnosis": data.diagnosis.model_dump() if data.diagnosis else None,
        "findings": [{"author": a, **f.model_dump()} for a, f in data.findings],
        "agents": {
            name: {
                "status": r.status,
                "model": r.model,
                "prompt_version": r.prompt_version,
                "steps": r.steps,
                "result": r.result,
            }
            for name, r in data.runs.items()
        },
        "usage": data.usage,
    }


@router.get("/{incident_id}/agents")
async def get_agents(
    incident_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
) -> list[dict[str, Any]]:
    """The call tree: each agent run, its model steps (time, tokens, cost) and each step's tool calls."""
    async with request.app.state.engine.connect() as conn:
        if await incidents.get(conn, incident_id, tenant_id=principal.tenant_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
        return await investigations.agent_tree(conn, incident_id)


@router.get("/{incident_id}/report", response_class=PlainTextResponse)
async def get_report(
    incident_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
) -> PlainTextResponse:
    _, data = await _investigation(request, incident_id, principal.tenant_id)
    return PlainTextResponse(render_markdown(data), media_type="text/markdown; charset=utf-8")
