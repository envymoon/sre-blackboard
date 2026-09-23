"""Shared runtime state for ops visibility. The Harness owns scheduling; this module is a
read-mostly mirror of what the Harness knows: queue depths, agent registry, active
incidents and their pipeline stage, token spend. Served by GET /api/v1/status for the
live ops board (docs/live.html). Time-based stage advancement keeps the board alive
without background threads: each poll recomputes stage from elapsed wall-clock time."""
from __future__ import annotations
import time

STAGES = [
    "normalize_and_deduplicate",
    "verify_impact",
    "coordinator_planning",
    "initial_task_dispatch",
    "dynamic_evidence_collection",
    "coordinator_synthesis",
    "safety_review",
    "human_approval_if_required",
    "recovery_verification",
    "resolve_or_escalate",
]
GATE_STAGE = 7  # index of human_approval_if_required: advancement pauses here until approved
STAGE_SECONDS = 6  # demo pacing: one stage per 6s of wall-clock time
STAGE_COST = 0.013  # ~$0.13 per full P0 run, matches the landing-page simulation

# Static agent registry: role, capability, model route. Status is derived per snapshot.
AGENTS = [
    {"name": "coordinator", "role": "planning + dispatch", "model": "muse-spark-1.3 (xhigh)"},
    {"name": "collector-metrics", "role": "metrics evidence", "model": "gemini-3.8-flash (medium)"},
    {"name": "collector-logs", "role": "log evidence", "model": "gemini-3.8-flash (medium)"},
    {"name": "collector-trace", "role": "trace evidence", "model": "gemini-3.8-flash (medium)"},
    {"name": "collector-change", "role": "change-event evidence", "model": "gemini-3.8-flash (medium)"},
    {"name": "retriever", "role": "RAG Top20+20 -> Top12", "model": "gemini-3.8-flash (medium)"},
    {"name": "ranker", "role": "reranker + RRF k=60", "model": "Qwen3-Reranker-0.6B"},
    {"name": "diagnoser", "role": "root-cause candidate", "model": "muse-spark-1.3 (xhigh)"},
    {"name": "safety", "role": "R0-R4 classification", "model": "muse-spark-1.3 (xhigh)"},
    {"name": "reporter", "role": "Sec.18 10-section report", "model": "gemini-3.8-flash (medium)"},
]

# Stages each agent is "working" during (by stage index); otherwise idle.
AGENT_ACTIVE_STAGES = {
    "coordinator": [2, 3],
    "collector-metrics": [4], "collector-logs": [4],
    "collector-trace": [4], "collector-change": [4],
    "retriever": [4, 5], "ranker": [5],
    "diagnoser": [5, 6], "safety": [6, 7],
    "reporter": [9],
}


class RuntimeState:
    """Process-global Harness mirror. All methods are cheap and side-effect free
    except record_* / approve, which mutate."""

    def __init__(self):
        from .queue import PriorityQueue
        self.started_at = time.time()
        self.queue = PriorityQueue()
        self.incidents: dict = {}  # incident_id -> record
        self.spent = 0.0
        self.budget = 0.50
        self.tasks_done = 0

    # -- mutations ---------------------------------------------------------
    def record_alert(self, result: dict, alert: dict):
        """Mirror POST /api/v1/alerts into queue + incident registry. Returns the
        incident record for OK, None otherwise. Never alters result."""
        if result.get("status") != "OK":
            return None
        incident_id = result["incident_id"]
        priority = alert.get("priority", "P3")
        from .queue import make_message
        self.queue.enqueue(make_message(incident_id, f"task-{incident_id}", "Problem", priority))
        now = time.time()
        rec = {"incident_id": incident_id, "service": alert.get("service_id", "?"),
               "priority": priority, "stage_idx": 0, "status": "ACTIVE",
               "approved": False, "cost": 0.0, "started_at": now, "updated_at": now}
        self.incidents[incident_id] = rec
        return rec

    def approve(self, incident_id: str) -> bool:
        rec = self.incidents.get(incident_id)
        if rec and rec["status"] == "AWAITING_APPROVAL":
            rec["approved"] = True
            rec["gate_released_at"] = time.time()
            rec["updated_at"] = time.time()
            return True
        return False

    # -- advancement --------------------------------------------------------
    def _advance(self, rec: dict):
        base = rec.get("gate_released_at", rec["started_at"]) if rec.get("approved") else rec["started_at"]
        offset = GATE_STAGE + 1 if rec.get("approved") else 0
        elapsed = time.time() - base
        idx = min(len(STAGES) - 1, offset + int(elapsed // STAGE_SECONDS))
        if not rec.get("approved") and idx >= GATE_STAGE:
            idx = GATE_STAGE
            if rec["status"] == "ACTIVE":
                rec["status"] = "AWAITING_APPROVAL"
                # The safety stage files the R3 approval card the board will approve.
                from .auth import APPROVALS
                plan_id = f"AP-{rec['incident_id']}"
                APPROVALS.create_card(rec["incident_id"], plan_id, rec["priority"],
                                      rec["service"], "ck-4821", "ck-4819",
                                      ["LOG-7782"], "rollback deploy")
                rec["action_plan_id"] = plan_id
        elif rec.get("approved") and idx >= len(STAGES) - 1 and rec["status"] != "RESOLVED":
            rec["status"] = "RESOLVED"
        if idx != rec["stage_idx"]:
            self.tasks_done += 1
        rec["stage_idx"] = idx
        rec["cost"] = round(STAGE_COST * (idx + 1), 3)
        rec["updated_at"] = time.time()

    # -- snapshot ------------------------------------------------------------
    def snapshot(self) -> dict:
        for rec in self.incidents.values():
            if rec["status"] != "RESOLVED":
                self._advance(rec)
        self.spent = round(sum(r["cost"] for r in self.incidents.values()), 3)
        live = [r["stage_idx"] for r in self.incidents.values()
                if r["status"] in ("ACTIVE", "AWAITING_APPROVAL")]
        cur = max(live) if live else -1
        agents = []
        for a in AGENTS:
            stages = AGENT_ACTIVE_STAGES.get(a["name"], [])
            working = cur in stages
            agents.append({**a, "status": "working" if working else "idle",
                           "current_stage": STAGES[cur] if working else None})
        return {
            "harness": {"uptime_s": round(time.time() - self.started_at, 1),
                        "queue": {p: len(self.queue.ready[p]) for p in ("P0", "P1", "P2", "P3")},
                        "leases": len(self.queue.leases),
                        "dead_letter": len(self.queue.dead_letter),
                        "tasks_done": self.tasks_done,
                        "ledger": {"spent": self.spent, "budget": self.budget}},
            "agents": agents,
            "incidents": [{**r, "stage": STAGES[r["stage_idx"]],
                           "stage_no": r["stage_idx"] + 1} for r in self.incidents.values()],
        }


STATE = RuntimeState()
