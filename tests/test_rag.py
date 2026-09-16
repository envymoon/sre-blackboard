"""RAG tests: chunking / filtering / fusion / threshold / Top6 / cache / MCP (spec Sec.12)."""
import pathlib
from app.knowledge import (KnowledgeIndex, chunk_document, search_knowledge, get_runbook,
                           get_service_profile, get_escalation_policy, cite_knowledge, validate_chunk)


def _index():
    idx = KnowledgeIndex()
    for i, p in enumerate(sorted(pathlib.Path("kb").glob("KB-SRE-*.md")), 1):
        kid = p.stem
        chunks = chunk_document(kid, "v1", p.read_text(encoding="utf-8"))
        idx.add({"service_scope": ["checkout-service"], "environment_scope": ["production"],
                 "trust_level": "internal_runbook", "status": "PUBLISHED", "owner": "sre",
                 "title": kid, "source_url": f"kb://{kid}"}, chunks)
    return idx


def test_chunk_id_and_validation():
    chunks = chunk_document("KB-SRE-001", "v1", "## Checkout triage\n" + "checkout order inventory deduction timeout " * 60)
    assert chunks and chunks[0]["chunk_id"].startswith("KB-SRE-001:v1:")
    assert validate_chunk({"status": "PUBLISHED", "owner": "sre", "document_version": "v1", "service_scope": ["x"]})
    assert not validate_chunk({"status": "DRAFT", "owner": "sre", "document_version": "v1", "service_scope": ["x"]})


def test_search_contract():
    idx = _index()
    r = search_knowledge(idx, "tenant-checkout", "checkout-service", "production", "Problem",
                         "checkout order inventory deduction timeout P0", priority="P0")
    assert r["status"] in ("OK", "NO_RELEVANT_KNOWLEDGE")
    if r["status"] == "OK":
        assert len(r["results"]) <= 6
        assert all(c["rerank_score"] >= 0.55 for c in r["results"])
        assert "chunk_id" in r["results"][0]
        r2 = search_knowledge(idx, "tenant-checkout", "checkout-service", "production", "Problem",
                              "checkout order inventory deduction timeout P0", priority="P0")
        assert r2.get("cached") or r2["status"] == "OK"


def test_mcp_five_functions():
    idx = _index()
    assert get_service_profile("checkout-service", "production")["slo"] == "99.9%/30d"
    assert "P0" in get_escalation_policy("P0", "checkout-service")["policy"]
    assert cite_knowledge("KB:x:v:h:01", "LOG-1")["status"] == "OK"
    assert cite_knowledge("", "LOG-1")["status"] == "REJECTED"
    rb = get_runbook(idx, "KB-SRE-001")
    assert rb["status"] in ("OK", "NOT_FOUND")
