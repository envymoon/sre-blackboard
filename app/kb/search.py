"""Hybrid retrieval: full text and vectors, fused by rank, reranked, thresholded.

    BM25 path   ts_rank_cd over the tsvector, top 20 ─┐
                                                      ├─ RRF (k=60) ─ top 20 ─ cross-encoder ─ >= min_score ─ top k
    vector path cosine distance over the embedding, top 20 ─┘

Each path is one SQL query, and each filters on ``tenant_id`` in its WHERE clause: there is
no way to search without a tenant, and nothing outside it can be returned. The vector path
also requires the chunk to have been embedded by the model the query is embedded with.

RRF adds 1 / (k + rank) over the lists a chunk appears in. It uses ranks only, so it needs no
calibration between a ts_rank_cd score (unbounded, length-normalised term weights) and a
cosine distance; k=60 keeps one list's top hit from outvoting agreement between the two.

``ts_rank_cd`` is cover density ranking, not true BM25: Postgres has no inverse document
frequency. The query is OR of its terms (plain ``plainto_tsquery`` would AND them and
miss most natural-language questions); the evaluation measures what that costs.

The modes exist for the ablation (eval/retrieval): ``bm25``, ``vector``, ``hybrid`` (RRF, no
reranker, no threshold) and ``rerank`` (the full pipeline, the default).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.kb.models import Embedder, Reranker

log = logging.getLogger(__name__)

Mode = Literal["bm25", "vector", "hybrid", "rerank"]
MODES: tuple[Mode, ...] = ("bm25", "vector", "hybrid", "rerank")
RRF_K = 60
CANDIDATES = 20
RERANK_TIMEOUT_S = 5.0

_TERM = re.compile(r"[A-Za-z0-9]+")

_COLUMNS = """
    c.chunk_id, c.headings, c.sections, c.content, d.doc_id, d.kind, d.title, d.services, d.version
"""

BM25_SQL = text(
    f"""
    SELECT {_COLUMNS}, ts_rank_cd(c.tsv, q.query, 1) AS score
    FROM kb_chunks c
    JOIN kb_documents d ON d.id = c.document_id,
         to_tsquery('english', :query) AS q(query)
    WHERE c.tenant_id = :tenant AND d.tenant_id = :tenant
      AND c.tsv @@ q.query
      AND (CAST(:services AS text[]) IS NULL OR d.services && CAST(:services AS text[]))
    ORDER BY score DESC, c.chunk_id
    LIMIT :limit
    """
)

VECTOR_SQL = text(
    f"""
    SELECT {_COLUMNS}, 1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
    FROM kb_chunks c
    JOIN kb_documents d ON d.id = c.document_id
    WHERE c.tenant_id = :tenant AND d.tenant_id = :tenant
      AND d.embedder = :embedder
      AND (CAST(:services AS text[]) IS NULL OR d.services && CAST(:services AS text[]))
    ORDER BY c.embedding <=> CAST(:embedding AS vector), c.chunk_id
    LIMIT :limit
    """
)


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    kind: str
    title: str
    version: str
    headings: list[str]
    sections: list[str]
    content: str
    services: list[str]
    score: float = 0.0  # the final ordering key: rerank score, RRF score, or the path's own
    ranks: dict[str, int] = field(default_factory=dict)  # path -> 1-based rank in that path


@dataclass
class Result:
    hits: list[Hit]
    mode: Mode
    # The reranker failed or took too long: the fused order was used, without a threshold.
    degraded: bool = False

    @property
    def status(self) -> str:
        return "ok" if self.hits else "no_relevant_knowledge"


def or_query(query: str) -> str | None:
    """``a | b | c`` for to_tsquery: every term may match, none has to."""
    terms = sorted({t.lower() for t in _TERM.findall(query)})
    return " | ".join(terms) if terms else None


def rrf(*ranked: Sequence[Hit], k: int = RRF_K) -> list[Hit]:
    by_id: dict[str, Hit] = {}
    scores: dict[str, float] = {}
    for hits in ranked:
        for rank, hit in enumerate(hits, 1):
            kept = by_id.setdefault(hit.chunk_id, hit)
            kept.ranks |= hit.ranks
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
    fused = sorted(by_id.values(), key=lambda h: (-scores[h.chunk_id], h.chunk_id))
    for h in fused:
        h.score = scores[h.chunk_id]
    return fused


class KnowledgeBase:
    def __init__(
        self,
        engine: AsyncEngine,
        embedder: Embedder,
        reranker: Reranker | None = None,
        *,
        min_score: float = 0.05,
        candidates: int = CANDIDATES,
        rerank_timeout_s: float = RERANK_TIMEOUT_S,
    ) -> None:
        self.engine = engine
        self.embedder = embedder
        self.reranker = reranker
        self.min_score = min_score
        self.candidates = candidates
        self.rerank_timeout_s = rerank_timeout_s

    async def search(
        self,
        tenant_id: str,
        query: str,
        *,
        k: int = 5,
        mode: Mode = "rerank",
        services: Sequence[str] | None = None,
    ) -> Result:
        if not tenant_id:
            raise ValueError("a knowledge search needs a tenant")
        if mode == "rerank" and self.reranker is None:
            raise ValueError("mode 'rerank' needs a reranker")
        svc = list(services) if services else None
        bm25 = await self._bm25(tenant_id, query, svc) if mode != "vector" else []
        vector = await self._vector(tenant_id, query, svc) if mode != "bm25" else []
        if mode == "bm25":
            return Result(bm25[:k], mode)
        if mode == "vector":
            return Result(vector[:k], mode)
        fused = rrf(bm25, vector)[: self.candidates]
        if mode == "hybrid":
            return Result(fused[:k], mode)
        return await self._rerank(query, fused, k)

    async def _bm25(self, tenant_id: str, query: str, services: list[str] | None) -> list[Hit]:
        q = or_query(query)
        if q is None:
            return []
        params = {"tenant": tenant_id, "query": q, "services": services, "limit": self.candidates}
        return await self._fetch(BM25_SQL, params, "bm25")

    async def _vector(self, tenant_id: str, query: str, services: list[str] | None) -> list[Hit]:
        embedding = await asyncio.to_thread(self.embedder.query, query)
        params = {
            "tenant": tenant_id,
            "embedding": "[" + ",".join(f"{x:.7f}" for x in embedding) + "]",
            "embedder": self.embedder.name,
            "services": services,
            "limit": self.candidates,
        }
        return await self._fetch(VECTOR_SQL, params, "vector")

    async def _fetch(self, sql: TextClause, params: Mapping[str, Any], path: str) -> list[Hit]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sql, params)).mappings().all()
        return [
            Hit(
                chunk_id=r["chunk_id"],
                doc_id=r["doc_id"],
                kind=r["kind"],
                title=r["title"],
                version=r["version"],
                headings=list(r["headings"]),
                sections=list(r["sections"]),
                content=r["content"],
                services=list(r["services"]),
                score=float(r["score"]),
                ranks={path: i},
            )
            for i, r in enumerate(rows, 1)
        ]

    async def _rerank(self, query: str, fused: list[Hit], k: int) -> Result:
        assert self.reranker is not None
        if not fused:
            return Result([], "rerank")
        passages = [f"{h.title} > {' / '.join(h.headings)}\n{h.content}" for h in fused]
        try:
            scores = await asyncio.wait_for(
                asyncio.to_thread(self.reranker.scores, query, passages), timeout=self.rerank_timeout_s
            )
        except Exception as e:  # a reranker failure costs precision, not the answer
            log.warning("reranker failed, using the fused order: %r", e)
            return Result(fused[:k], "rerank", degraded=True)
        for h, s in zip(fused, scores, strict=True):
            h.score = s
        ranked = sorted(fused, key=lambda h: (-h.score, h.chunk_id))
        return Result([h for h in ranked if h.score >= self.min_score][:k], "rerank")
