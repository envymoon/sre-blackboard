"""Ingress/Worker tests."""

from app.api import normalize
from app.persistence import Store
from app.queue import PriorityQueue, make_message
from app.worker import process_one, recovery_job


def test_ingress_validation_and_idempotency():
    assert normalize({})["status"] == "REJECTED"
    a = {
        "alert_id": "a1",
        "tenant_id": "t1",
        "priority": "P0",
        "service_id": "checkout-service",
        "alert_name": "X",
        "environment": "production",
        "auth": {"tenant_id": "t1"},
    }
    r1 = normalize(dict(a))
    assert r1["status"] == "OK"
    assert normalize(dict(a))["status"] == "DEDUP_MERGED"


def test_worker_and_recovery():
    q = PriorityQueue()
    q.enqueue(make_message("i1", "t-p0", "Problem", "P0"))
    assert process_one(q)["task_id"] == "t-p0"
    s = Store()
    s.save_incident("inc-9", "t1", "Problem", "P1", {})
    s.save_task("t-9", "inc-9", "t1", "problem-alert-skill", "P1")
    q2 = PriorityQueue()
    out = recovery_job(s, q2)
    assert out["rebuilt"] == 1
