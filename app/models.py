"""Terms and data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Task:
    task_id: str
    incident_id: str
    alert_type: str  # Problem/Event/Business/Host
    priority: str  # P0-P3
    capability: str  # logs/metrics/trace/change/code/host/business...
    service: str = "checkout-service"
    time_window: dict[str, Any] = field(default_factory=dict)
    evidence_gap: str = ""
    status: str = "READY"  # READY->CLAIMED->RUNNING->ARTIFACT_PUBLISHED->COMPLETED
    lease_until: float = 0.0
    risk_level: str = "R0"


@dataclass
class Artifact:
    incident_id: str
    producer: str
    artifact_type: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = "HYPOTHESIS"
    gaps: list[str] = field(default_factory=list)
    next_task_proposals: list[dict[str, Any]] = field(default_factory=list)
    version: str = "v1"
    schema_version: str = "ArtifactV1"
    producer_skill_version: str = "v1"
    context_version: str = "ctx-v1"

    def validate(self) -> bool:
        # facts/hypotheses/evidence_refs/confidence/gaps/next_task_proposals are required
        return not (self.confidence == "CONFIRMED" and not self.evidence_refs)


@dataclass
class IncidentState:
    incident_id: str
    stage: str = "normalize_and_deduplicate"
    tasks: dict[str, Task] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    remaining_budget: dict[str, int] = field(default_factory=dict)
    next_step: str = ""


@dataclass
class Checkpoint:
    key: str  # incident_id+task_id+attempt+step
    payload: dict[str, Any] = field(default_factory=dict)
