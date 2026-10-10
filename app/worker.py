"""Harness Worker: dispatch + lease renewal + recovery."""

from __future__ import annotations

from .harness import AgentHarness
from .queue import PriorityQueue
from .runtime import AgentRuntime

harness = AgentHarness(runtime=AgentRuntime())


def process_one(queue: PriorityQueue, worker_id: str = "worker-1"):
    msg = queue.pop_next()
    if msg is None:
        return None
    lease = queue.claim(msg, worker_id)
    queue.renew(msg["task_id"])  # renew once on first pass, demonstrating the 30s renewal contract
    return {"task_id": msg["task_id"], "fencing_token": lease["fencing_token"]}


def recovery_job(store, queue: PriorityQueue) -> dict:
    """Requeue expired leases at original priority (queue.expire_leases) + rebuild READY tasks from the store (store.rebuild_queue)."""
    requeued = queue.expire_leases()
    rebuilt = store.rebuild_queue(queue)
    return {"requeued": requeued, "rebuilt": rebuilt}


if __name__ == "__main__":
    print("harness-worker ready: intelligent-sre-collaboration-assistant v2.0.0")
