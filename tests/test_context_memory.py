"""Context + memory tests. Maps to spec Sec.10/Sec.11."""
from app.context import compress_pipeline, partitions, INPUT_BUDGETS
from app.memory import WorkingMemory, VerifiedMemory, read_order


def test_partitions_no_borrow():
    assert partitions("P0")["logs"] == 6000 and partitions("P2")["logs"] == 3500
    assert INPUT_BUDGETS["P0"] == 28000


def test_compress_truncates_and_validates():
    evs = [{"kind": "log", "evidence_id": f"LOG-{i}", "text": "x" * 2000, "samples": [1, 2, 3, 4],
            "service_match": True, "time_score": 25, "trace_link": True, "severity": 15, "p0_related": True} for i in range(20)]
    r = compress_pipeline({}, evs, "P0")
    assert r["status"] == "OK"
    assert len([f for f in r["envelope"]["facts"] if f["evidence_id"].startswith("LOG-")]) <= 20
    bad = compress_pipeline({}, [{"kind": "log", "text": "no id"}], "P0")
    assert bad["status"] == "CONTEXT_INVALID"


def test_memory_promotion_gates():
    w, v = WorkingMemory(), VerifiedMemory()
    w.write_incident("inc-1", {"p": "P0"})
    assert w.read_hot("inc-1")
    bad = v.promote({"evidence_ids": [], "verified": True})
    assert bad["status"] == "REJECTED"
    low = v.promote({"evidence_ids": ["E1"], "verified": True, "service": "s", "environment": "e",
                     "human_confirmed": True, "desensitized": True, "confidence": "LOW"})
    assert low["status"] == "REJECTED"
    ok = v.promote({"evidence_ids": ["E1"], "verified": True, "service": "s", "environment": "e",
                    "human_confirmed": True, "desensitized": True})
    assert ok["status"] == "CANDIDATE"
    assert v.publish(0, "owner")["status"] == "PUBLISHED"
    order = read_order({"incident_id": "inc-1"}, w, v, lambda: ["kb"])
    assert order["hot"] and order["adopted_experience"] and order["knowledge"] == ["kb"]
