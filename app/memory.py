"""Two-layer memory. Maps to spec Sec.11: Redis working memory for the live incident +
MySQL verified-experience memory. Read order: task + Redis hot -> MySQL adopted -> RAG published."""
from __future__ import annotations
import time

WORKING_TTL_S = 6 * 3600
CLOSED_RETAIN_S = 24 * 3600
CANDIDATE_REVIEW_DAYS = 30
PUBLISHED_RETAIN_DAYS = 365


class WorkingMemory:
    def __init__(self):
        self.ctx: dict = {}
        self.artifacts: dict = {}

    def write_incident(self, incident_id: str, snapshot: dict):
        self.ctx[incident_id] = {"snapshot": snapshot, "expires_at": time.time() + WORKING_TTL_S}

    def append_artifact(self, incident_id: str, artifact_id: str, summary: str):
        lst = self.artifacts.setdefault(incident_id, [])
        lst.append({"artifact_id": artifact_id, "summary": summary[:800]})
        self.artifacts[incident_id] = lst[-20:]  # keep the latest 20 only

    def read_hot(self, incident_id: str) -> dict:
        c = self.ctx.get(incident_id)
        if not c or c["expires_at"] < time.time():
            return {}
        return c


class VerifiedMemory:
    def __init__(self):
        self.candidates: list = []
        self.published: list = []

    def promote(self, incident: dict) -> dict:
        """Sec.11.2 six conditions + Sec.11.3 anti-pollution: reject when any is missing."""
        required = ["evidence_ids", "verified", "service", "environment", "human_confirmed", "desensitized"]
        if not all(incident.get(k) for k in required):
            return {"status": "REJECTED", "reason": "promotion conditions unmet"}
        if incident.get("confidence") == "LOW":
            return {"status": "REJECTED", "reason": "low confidence cannot enter long-term memory"}
        cand = {**incident, "status": "candidate", "review_within_days": CANDIDATE_REVIEW_DAYS}
        self.candidates.append(cand)
        return {"status": "CANDIDATE", "id": len(self.candidates) - 1}

    def publish(self, idx: int, reviewer: str) -> dict:
        cand = self.candidates[idx]
        pub = {**cand, "status": "published", "reviewer": reviewer, "retain_days": PUBLISHED_RETAIN_DAYS}
        self.published.append(pub)
        return {"status": "PUBLISHED"}


def read_order(task: dict, working: WorkingMemory, verified: VerifiedMemory, rag_fn) -> dict:
    """Live evidence outranks old experience; old experience only suggests routes, never overrides current facts."""
    hot = working.read_hot(task.get("incident_id", ""))
    adopted = verified.published[-1] if verified.published else None
    kb = rag_fn() if rag_fn else []
    return {"task": task, "hot": hot, "adopted_experience": adopted, "knowledge": kb}
