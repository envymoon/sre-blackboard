"""RAG dual-path recall + rerank. Maps to spec Sec.12.8: RRF k=60 Top30 ->
rules -> cross-encoder Top12, threshold 0.55, P95 < 200ms."""
from __future__ import annotations
import time


def rrf_fuse(rank_bm25: dict, rank_vec: dict, k: int = 60):
    scores = {}
    for cid, r in rank_bm25.items():
        scores[cid] = scores.get(cid, 0) + 1.0 / (k + r)
    for cid, r in rank_vec.items():
        scores[cid] = scores.get(cid, 0) + 1.0 / (k + r)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def rule_rerank(candidates, alert: dict):
    def bonus(c):
        s = 0
        if c.get("service_scope") == alert.get("service_id"):
            s += 20
        if c.get("environment") == alert.get("environment"):
            s += 15
        if c.get("trust_level") == "internal_runbook":
            s += 10
        return s
    return sorted(candidates, key=lambda c: (c.get("base_score", 0) + bonus(c)), reverse=True)


def qwen_rerank_stub(query: str, candidates, timeout_ms: int = 300, threshold: float = 0.55):
    """alert-reranker-qwen3-0.6b-v1 placeholder: on timeout degrade to rule Top6 and flag reranker_degraded=true."""
    t0 = time.time()
    out = [c for c in candidates[:12] if c.get("rerank_score", 0.6) >= threshold]
    degraded = (time.time() - t0) * 1000 > timeout_ms
    if degraded:
        return candidates[:6], True
    return out, False
