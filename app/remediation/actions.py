"""The action catalogue: everything a plan can ask the executor to do (ADR-0007).

A plan is structured data, never a command line. Each action here has a risk level, an
inverse (how to undo it, decided here and not by the model) and an implementation in the
executor. Anything else, R4 included, cannot be expressed: the model's output schema only
has these branches, and a plan submitted through the API is validated against the same
models.

One Deployment per service, named after it, as in the demo: an action's ``deployment`` is
also the service it repairs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Risk = Literal["R0", "R1", "R2", "R3", "R4"]
RISKS: tuple[Risk, ...] = ("R0", "R1", "R2", "R3", "R4")
# A Kubernetes object name (RFC 1123 label).
K8S_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"


def risk_rank(risk: Risk) -> int:
    return RISKS.index(risk)


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment: str = Field(pattern=K8S_NAME, description="The Deployment to act on (named after its service)")
    evidence_ids: list[str] = Field(
        min_length=1, max_length=6, description="ev_... ids of the results that justify this action"
    )
    reason: str = Field(max_length=400, description="Why this action fixes the root cause, for the approver")


class RolloutUndo(_Action):
    """Roll a Deployment back to an earlier revision (the bad release is undone)."""

    action: Literal["rollout_undo"]
    to_revision: int | None = Field(
        None, ge=1, description="The revision to return to; omit for the one before the current"
    )


class RestartDeployment(_Action):
    """Restart every pod of a Deployment (rolling)."""

    action: Literal["restart_deployment"]


class ScaleDeployment(_Action):
    """Set a Deployment's replica count."""

    action: Literal["scale_deployment"]
    replicas: int = Field(ge=1, le=10)


# A plain union (anyOf in the JSON schema, which every structured-output implementation takes);
# the ``action`` literal picks the branch.
Action = RolloutUndo | RestartDeployment | ScaleDeployment
ActionKind = Literal["rollout_undo", "restart_deployment", "scale_deployment"]


@dataclass(frozen=True)
class ActionInfo:
    risk: Risk
    inverse: str
    # A rollout_undo needs proof there is a release to undo: a cited result of one of these
    # tools, about the same Deployment.
    evidence_tools: tuple[str, ...] = ()


CATALOGUE: dict[str, ActionInfo] = {
    "rollout_undo": ActionInfo(
        "R3", "roll the Deployment back to the revision it was on before", ("rollout_history", "deploy_diff")
    ),
    "restart_deployment": ActionInfo("R2", "none needed: a rolling restart of a stateless service"),
    "scale_deployment": ActionInfo("R2", "scale back to the replica count it had before"),
}
