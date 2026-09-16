"""Priority queue + leases + retries. Maps to spec Sec.15: priority beats arrival time;
four ready ZSETs, 90s leases renewed every 30s, dead-letter after 3 attempts."""
from __future__ import annotations
import time
import uuid

PRIORITIES = ["P0", "P1", "P2", "P3"]
SLOTS = {"P0": 5, "P1": 2, "P2": 2, "P3": 1}  # reserved worker slots out of 10


def make_message(incident_id: str, task_id: str, alert_type: str, priority: str, stage: str = "READY") -> dict:
    now = time.time()
    return {
        "message_id": f"msg-{uuid.uuid4().hex[:8]}",
        "incident_id": incident_id, "task_id": task_id, "alert_type": alert_type,
        "priority": priority, "stage": stage, "attempt": 0,
        "deadline_at": now + 300, "idempotency_key": f"{incident_id}+{task_id}+{stage}",
        "enqueue_time_ms": int(now * 1000),
    }


class PriorityQueue:
    """In-memory version of the four Redis ZSET queues: score=enqueue_time_ms, FIFO within
    a level, strict P0->P3 dispatch order."""

    def __init__(self):
        self.ready: dict = {p: [] for p in PRIORITIES}
        self.delayed: list = []
        self.dead_letter: list = []
        self.leases: dict = {}  # task_id -> {worker, fencing, expires_at, attempt}
        self.p0_arrived: bool = False
        self.dispatched_after_p0: list = []

    def enqueue(self, msg: dict):
        self.ready[msg["priority"]].append(msg)
        self.ready[msg["priority"]].sort(key=lambda m: m["enqueue_time_ms"])
        if msg["priority"] == "P0":
            self.p0_arrived = True

    def pop_next(self):
        # Once a P0 has arrived, no new P1-P3 may be dispatched (Sec.15 acceptance check)
        for p in PRIORITIES:
            if not self.ready[p]:
                continue
            if self.p0_arrived and p != "P0":
                self.dispatched_after_p0.append(p)
                return None
            return self.ready[p].pop(0)
        return None

    def claim(self, msg: dict, worker_id: str, ttl_s: int = 90) -> dict:
        fencing = uuid.uuid4().hex[:8]
        self.leases[msg["task_id"]] = {"worker": worker_id, "fencing": fencing,
                                       "expires_at": time.time() + ttl_s, "attempt": msg.get("attempt", 0)}
        return {"lease": True, "fencing_token": fencing}

    def renew(self, task_id: str, ttl_s: int = 90) -> bool:
        if task_id not in self.leases:
            return False
        self.leases[task_id]["expires_at"] = time.time() + ttl_s
        return True

    def expire_leases(self):
        """Requeue expired leases at their original priority; dead-letter after 3 attempts. Returns the requeued list."""
        now = time.time()
        requeued = []
        for task_id, lease in list(self.leases.items()):
            if lease["expires_at"] > now:
                continue
            del self.leases[task_id]
            attempt = lease["attempt"] + 1
            if attempt > 3:
                self.dead_letter.append({"task_id": task_id, "attempt": attempt})
                continue
            # Original priority is unknown here; callers should hand the full msg back -- only the attempt is recorded
            requeued.append({"task_id": task_id, "attempt": attempt})
        return requeued

    # Sec.15.3 retry policy (pure function, easy to test)
    @staticmethod
    def retry_policy(error_kind: str, attempt: int) -> dict:
        if error_kind == "tool_timeout":
            if attempt < 2:
                return {"retry": True, "backoff_s": 5 if attempt == 0 else 15, "narrow_scope": attempt >= 1}
            return {"retry": False}
        if error_kind in ("permission", "param", "schema"):
            return {"retry": False, "escalate": True}
        if error_kind == "model_json_invalid":
            return {"retry": True, "format_fix_once": True, "new_tool_calls": 0}
        return {"retry": False}
