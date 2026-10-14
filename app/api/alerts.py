"""Alert ingest endpoint (Alertmanager webhook receiver)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from app.core.security import Principal, require
from app.ingest import AlertmanagerWebhook, ingest

router = APIRouter(prefix="/api/v1", tags=["alerts"])


class IngestedAlertOut(BaseModel):
    fingerprint: str
    status: str
    incident_id: UUID | None
    created: bool


class IngestResponse(BaseModel):
    alerts: list[IngestedAlertOut]


@router.post("/alerts", status_code=status.HTTP_202_ACCEPTED)
async def receive_alerts(
    webhook: AlertmanagerWebhook,
    request: Request,
    principal: Annotated[Principal, Depends(require("ingest"))],
) -> IngestResponse:
    """Accept an Alertmanager webhook. Safe to retry: duplicates merge into the open incident."""
    async with request.app.state.engine.begin() as conn:
        results = await ingest(
            conn,
            webhook,
            tenant_id=principal.tenant_id,
            actor=principal.subject,
            settings=request.app.state.settings,
        )
    return IngestResponse(alerts=[IngestedAlertOut(**r.__dict__) for r in results])
