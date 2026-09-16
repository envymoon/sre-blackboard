"""Terms and data contracts. Mirror index.md Sec.7.2, Sec.8.2, Sec.8.8, Sec.9.1.2."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Task:
    task_id: str
    incident_id: str
    alert_type: str  # Problem/Event/Business/Host
    priority: str  # P0-P3
    capability: str  # logs/metrics/trace/change/code/host/business...
    service: str = "checkout-service"
    time_window: Dict[str, Any] = field(default_factory=dict)
    evidence_gap: str = ""
    status: str = "READY"  # READY->CLAIMED->RUNNING->ARTIFACT_PUBLISHED->COMPLETED
    lease_until: float = 0.0
    risk_level: str = "R0"


@dataclass
class Artifact:
    incident_id: str
    producer: str
    artifact_type: str
    facts: List[Dict[str, Any]] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    confidence: str = "HYPOTHESIS"
    gaps: List[str] = field(default_factory=list)
    next_task_proposals: List[Dict[str, Any]] = field(default_factory=list)
    version: str = "v1"
    schema_version: str = "ArtifactV1"
    producer_skill_version: str = "v1"
    context_version: str = "ctx-v1"

    def validate(self) -> bool:
        # Sec.8.2: facts/hypotheses/evidence_refs/confidence/gaps/next_task_proposals are required
        if self.confidence == "CONFIRMED" and not self.evidence_refs:
            return False
        return True


@dataclass
class IncidentState:
    incident_id: str
    stage: str = "normalize_and_deduplicate"
    tasks: Dict[str, Task] = field(default_factory=dict)
    artifacts: List[Artifact] = field(default_factory=list)
    remaining_budget: Dict[str, int] = field(default_factory=dict)
    next_step: str = ""


@dataclass
class Checkpoint:
    key: str  # incident_id+task_id+attempt+step
    payload: Dict[str, Any] = field(default_factory=dict)
