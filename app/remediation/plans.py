"""A remediation plan: what the planner (or a responder) proposes, and the hash approvals bind to."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.remediation.actions import Action, ActionKind


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Alternative(_Model):
    action: ActionKind | Literal["none"] = Field(description="An action considered and not proposed")
    why_not: str = Field(max_length=300)


class RemediationPlan(_Model):
    """The planner's answer. An empty ``actions`` list means: nothing in the catalogue fixes this."""

    summary: str = Field(
        max_length=600, description="What the plan does and what should happen after, in two sentences"
    )
    actions: list[Action] = Field(max_length=2, description="Actions to run in order; empty if none would help")
    alternatives: list[Alternative] = Field(
        max_length=4, description="Other actions you considered, lower-risk ones in particular, and why not"
    )
    no_action_reason: str | None = Field(
        None, max_length=400, description="When actions is empty: why, and what a human should do instead"
    )

    def cited(self) -> set[str]:
        return {ev for a in self.actions for ev in a.evidence_ids}


def plan_hash(incident_id: UUID, seq: int, plan: RemediationPlan) -> str:
    """sha256 over everything an approval is an approval of. Any change, a new hash."""
    canonical = json.dumps(
        [str(incident_id), seq, plan.model_dump(mode="json")], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def describe(plan: RemediationPlan) -> list[dict[str, Any]]:
    """The actions as plain dicts, for audit payloads and API answers."""
    return [a.model_dump(mode="json") for a in plan.actions]
