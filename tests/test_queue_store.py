"""Queue + persistence tests. Maps to spec Sec.15/Sec.16: priority / preemption / leases / dead-letter / rebuild."""
from app.queue import PriorityQueue, make_message
from app.persistence import Store


def test_priority_p0_first():
    q = PriorityQueue()
    q.enqueue(make_message("i1", "t-p3", "Problem", "P3"))
    q.enqueue(make_message("i1", "t-p1", "Problem", "P1"))
    q.enqueue(make_message("i1", "t-p0", "Problem", "P0"))
    # First pop must be P0 (strict P0->P3); the P0-arrived flag then blocks lower priorities
    assert q.pop_next()["priority"] == "P0"
    assert q.pop_next() is None
    assert q.dispatched_after_p0  # records the blocked P1/P3 dispatches


def test_lease_expiry_and_deadletter():
    q = PriorityQueue()
    q.leases["t1"] = {"worker": "w1", "fencing": "f", "expires_at": 0.0, "attempt": 3}
    rq = q.expire_leases()
    assert rq == [] and len(q.dead_letter) == 1


def test_retry_policy():
    from app.queue import PriorityQueue as Q
    assert Q.retry_policy("tool_timeout", 0) == {"retry": True, "backoff_s": 5, "narrow_scope": False}
    assert Q.retry_policy("tool_timeout", 1)["narrow_scope"]
    assert Q.retry_policy("permission", 0)["escalate"]
    assert Q.retry_policy("model_json_invalid", 0)["format_fix_once"]


def test_persistence_and_rebuild():
    s = Store()
    s.save_incident("inc-1", "tenant-checkout", "Problem", "P0", {"a": 1})
    s.save_task("t-1", "inc-1", "tenant-checkout", "problem-alert-skill", "P0")
    s.append_trace("run-1", "inc-1", "t-1", "tenant-checkout", "log-agent", "elasticsearch.search_logs", 100, 50, 12, "ok")
    q = PriorityQueue()
    assert s.rebuild_queue(q) == 1
    assert q.pop_next()["task_id"] == "t-1"
