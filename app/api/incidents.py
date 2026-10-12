"""Read-only incident endpoints. Every query is scoped to the caller's tenant."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.engine import RowMapping

from app.core.security import Principal, require
from app.db import incidents
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
