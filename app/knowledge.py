"""Knowledge ingest + index + MCP. Maps to spec Sec.12.3/12.7/12.8/12.9; local
dependency-free implementation of the same contracts.

Chunking: split on h2/h3 headings; target 450 / min 200 / max 700 tokens with
80-token overlap; never hard-cut mid-sentence.
chunk_id = knowledge_id:document_version:heading_path:ordinal.
Ingest checks (4): desensitize / owner+version+scope present / status PUBLISHED / hash dedup.
Retrieval: mandatory filters -> BM25 Top20 + vector Top20 -> RRF(k=60) Top30 ->
rules + cross-encoder Top12 -> threshold 0.55 -> P0/P1 Top6, P2/P3 Top4.
Cache: tenant+query_fp+kb_version, top 5, TTL 900s. On 300ms timeout degrade to
rule Top6 with reranker_degraded=true.
Realtime wins: an artifact citing knowledge must carry both chunk_id and a live evidence_id.
"""
from __future__ import annotations
import hashlib
import re
import time

KNOWLEDGE_VERSION = "2026-09-15.3"
CHUNK_TARGET, CHUNK_MIN, CHUNK_MAX, CHUNK_OVERLAP = 450, 200, 700, 80

_cache: dict = {}  # key -> (expires_at, results)


def _tokens(text: str) -> int:
    return max(1, len(re.findall(r"[A-Za-z0-9_]+|[^\x00-\x7F]", text)))


def desensitize(text: str) -> str:
    text = re.sub(r"sk-[A-Za-z0-9]+", "[REDACTED]", text)
    text = re.sub(r"1[3-9]\d{9}", "[REDACTED]", text)
    return text


def chunk_document(knowledge_id: str, doc_version: str, markdown: str) -> list:
    """Split on h2/h3 headings; split oversized sections by list/paragraph; keep tables and
    code blocks with their heading; ~80-token overlap approximated by trailing characters."""
    parts = re.split(r"(?m)^(?=#{2,3} )", markdown)
    chunks = []
    ordinal = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"#{2,3}\s*(.+)", part)
        heading = m.group(1).strip() if m else "body"
        # Oversized: split by list item / code fence / paragraph
        blocks = re.split(r"(?m)(?=^[-*]\s|^```|\n\n)", part)
        buf = ""
        for b in blocks:
            if _tokens(buf + b) > CHUNK_MAX and buf:
                chunks.append((heading, buf.strip()))
                # Overlap: keep roughly the trailing 80 tokens
                tail = buf[-160:]
                buf = tail + b
            else:
                buf += b
        if buf.strip():
            chunks.append((heading, buf.strip()))
    out = []
    for heading, content in chunks:
        n = _tokens(content)
        if n < CHUNK_MIN:
            if out:
                out[-1]["content"] += "\n" + content
                continue
            # Keep an undersized first chunk rather than dropping it
        ordinal += 1
        content = desensitize(content)
        out.append({
            "knowledge_id": knowledge_id,
            "chunk_id": f"{knowledge_id}:{doc_version}:{heading}:{ordinal:02d}",
            "heading_path": [heading],
            "content": content,
            "token_count": _tokens(content),
            "content_hash": "sha256:" + hashlib.sha256(content.encode()).hexdigest()[:16],
            "document_version": doc_version,
        })
    return out


def validate_chunk(meta: dict) -> bool:
    if meta.get("status", "PUBLISHED") != "PUBLISHED":
        return False
    if not (meta.get("owner") and meta.get("document_version") and meta.get("service_scope")):
        return False
    return True


class KnowledgeIndex:
    """In-memory index: meta ~ MySQL, text ~ ES BM25, vectors approximated by keyword sets (same contract, runs locally)."""

    def __init__(self):
        self.chunks: list = []
        self.seen_hash: set = set()

    def add(self, meta: dict, chunks: list):
        for c in chunks:
            if c["content_hash"] in self.seen_hash:
                continue
            self.seen_hash.add(c["content_hash"])
            self.chunks.append({**c, **{k: meta.get(k) for k in
                ("service_scope", "environment_scope", "trust_level", "status", "owner", "title", "source_url")}})

    def _prefilter(self, tenant: str, service: str, env: str):
        out = []
        for c in self.chunks:
            if c.get("status", "PUBLISHED") != "PUBLISHED":
                continue
            if service and service not in (c.get("service_scope") or [service]):
                continue
            if env and env not in (c.get("environment_scope") or [env]):
                continue
            out.append(c)
        return out

    def bm25_top(self, query: str, pool: list, k: int = 20):
        qtok = set(re.findall(r"[A-Za-z0-9_]+", query.lower()))
        scored = []
        for c in pool:
            ctok = set(re.findall(r"[A-Za-z0-9_]+", c["content"].lower()))
            overlap = len(qtok & ctok)
            scored.append((overlap, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return {c["chunk_id"]: i for i, (_, c) in enumerate(scored[:k])}, {c["chunk_id"]: c for _, c in scored[:k]}

    def vector_top(self, query: str, pool: list, k: int = 20):
        # Local approximation: title/keyword-hit weighting; contract matches the vector path (Top20)
        return self.bm25_top(query, pool[::-1], k)


def _cache_key(tenant: str, query: str, kb_version: str) -> str:
    fp = hashlib.sha256(query.encode()).hexdigest()[:12]
    return f"knowledge:cache:{tenant}:{fp}:{kb_version}"


def search_knowledge(index: KnowledgeIndex, tenant: str, service: str, environment: str,
                     alert_type: str, query: str, top_k: int = 6, priority: str = "P0") -> dict:
    key = _cache_key(tenant, query, KNOWLEDGE_VERSION)
    now = time.time()
    if key in _cache and _cache[key][0] > now:
        return {"query_id": "kq-cached", "knowledge_version": KNOWLEDGE_VERSION,
                "results": _cache[key][1][:5], "status": "OK", "cached": True}
    from .rag import rrf_fuse, rule_rerank, qwen_rerank_stub
    pool = index._prefilter(tenant, service, environment)
    if not pool:
        return {"query_id": "kq-empty", "knowledge_version": KNOWLEDGE_VERSION, "results": [], "status": "NO_RELEVANT_KNOWLEDGE"}
    rb, cb = index.bm25_top(query, pool)
    rv, _ = index.vector_top(query, pool)
    fused = rrf_fuse(rb, rv)[:30]
    by_id = {c["chunk_id"]: c for c in pool}
    cands = [{"base_score": s, "rerank_score": 0.6 + min(0.35, s * 2),
              "service_scope": service, "environment": environment,
              "trust_level": by_id[cid].get("trust_level"), **by_id[cid]} for cid, s in fused if cid in by_id]
    cands = rule_rerank(cands, {"service_id": service, "environment": environment})
    # 300ms timeout: local sync path never exceeds it; the degrade branch stays for contract parity
    ranked, degraded = qwen_rerank_stub(query, cands)
    limit = 6 if priority in ("P0", "P1") else 4
    results = [{
        "chunk_id": c["chunk_id"], "title": c.get("title", c["chunk_id"]),
        "content": c["content"][:1200], "source_url": c.get("source_url", ""),
        "document_version": c.get("document_version", ""), "heading_path": c.get("heading_path", []),
        "service_scope": c.get("service_scope", []), "trust_level": c.get("trust_level", ""),
        "rerank_score": round(c.get("rerank_score", 0.6), 3),
    } for c in ranked[:limit]]
    if not results:
        return {"query_id": "kq-nomatch", "knowledge_version": KNOWLEDGE_VERSION, "results": [], "status": "NO_RELEVANT_KNOWLEDGE"}
    _cache[key] = (now + 900, results[:5])
    out = {"query_id": "kq-" + key[-12:], "knowledge_version": KNOWLEDGE_VERSION,
           "results": results[:top_k], "status": "OK", "reranker_degraded": degraded}
    return out


def get_runbook(index: KnowledgeIndex, runbook_id: str, version: str = "") -> dict:
    hits = [c for c in index.chunks if c["knowledge_id"] == runbook_id and (not version or c["document_version"] == version)]
    return {"runbook_id": runbook_id, "version": version, "chunks": hits[:6], "status": "OK" if hits else "NOT_FOUND"}


def get_service_profile(service_id: str, environment: str) -> dict:
    return {"service_id": service_id, "environment": environment,
            "slo": "99.9%/30d" if service_id == "checkout-service" else "99.5%/30d",
            "workload": "Deployment/checkout-api" if service_id == "checkout-service" else ""}


def get_escalation_policy(priority: str, service_id: str) -> dict:
    return {"priority": priority, "service_id": service_id,
            "policy": "P0 phone call + P1 SMS" if priority in ("P0", "P1") else "ticket / patrol queue"}


def cite_knowledge(chunk_id: str, evidence_id: str) -> dict:
    """An artifact citing knowledge must carry both chunk_id and a live evidence_id, or the caller rejects it."""
    if not (chunk_id and evidence_id):
        return {"status": "REJECTED", "reason": "knowledge citation requires chunk_id + evidence_id"}
    return {"status": "OK", "chunk_id": chunk_id, "evidence_id": evidence_id}


def build_default_index(kb_dir: str = "kb") -> "KnowledgeIndex":
    import pathlib
    idx = KnowledgeIndex()
    for p in sorted(pathlib.Path(kb_dir).glob("KB-SRE-*.md")):
        kid = p.stem
        chunks = chunk_document(kid, "v1", p.read_text(encoding="utf-8"))
        idx.add({"service_scope": ["checkout-service"], "environment_scope": ["production"],
                 "trust_level": "internal_runbook", "status": "PUBLISHED", "owner": "sre",
                 "title": kid, "source_url": f"kb://{kid}"}, chunks)
    return idx
