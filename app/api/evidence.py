"""Evidence snapshots, so every citation in a diagnosis can be opened and checked."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status

from app.core.security import Principal, require
from app.tools import evidence

router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])


@router.get("/{evidence_id}")
async def get_evidence(
    request: Request,
    principal: Annotated[Principal, Depends(require("viewer"))],
    evidence_id: Annotated[str, Path(pattern=r"^ev_[0-9a-f]{16}$")],
) -> dict[str, Any]:
    """The tool, its arguments, the raw result, its hash and the summary the model saw."""
    row = await evidence.load(request.app.state.engine, evidence_id, tenant_id=principal.tenant_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "evidence not found")
    return row
