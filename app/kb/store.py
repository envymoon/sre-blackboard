"""Ingest: documents -> chunks -> embeddings -> kb_documents / kb_chunks, per tenant.

Idempotent. A document whose content hash, embedding model and chunking match what is
stored is left alone; a changed one has its chunks replaced in one transaction, so a search never sees half
a document. With ``prune``, documents no longer in the directory are deleted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.schema import kb_chunks, kb_documents
from app.kb.chunking import DEFAULT_CHUNKING, PRESETS, ChunkParams, Document, chunk
from app.kb.models import Embedder

# bge-small reads at most 512 wordpieces; a chunk over this many tokens (words and
# punctuation, ~1.3 wordpieces each) is cut short for the vector path. BM25 sees all of it.
EMBED_TOKEN_LIMIT = 390


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    chunks: int = 0
    over_embed_limit: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {k: v for k, v in self.__dict__.items()}


async def ingest(
    engine: AsyncEngine,
    tenant_id: str,
    docs: Sequence[Document],
    embedder: Embedder,
    *,
    chunking: ChunkParams = PRESETS[DEFAULT_CHUNKING],
    prune: bool = False,
) -> IngestReport:
    report = IngestReport()
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                kb_documents.c.doc_id, kb_documents.c.content_hash, kb_documents.c.embedder, kb_documents.c.chunking
            ).where(kb_documents.c.tenant_id == tenant_id)
        )
        stored = {r.doc_id: (r.content_hash, r.embedder, r.chunking) for r in rows}

    for doc in docs:
        if stored.get(doc.id) == (doc.content_hash, embedder.name, chunking.name):
            report.unchanged.append(doc.id)
            continue
        pieces = chunk(doc, chunking)
        vectors = await asyncio.to_thread(embedder.documents, [c.text_for_embedding(doc.title) for c in pieces])
        report.over_embed_limit += [c.chunk_id for c in pieces if c.tokens > EMBED_TOKEN_LIMIT]
        async with engine.begin() as conn:
            values = {
                "kind": doc.kind,
                "title": doc.title,
                "services": list(doc.services),
                "version": doc.version,
                "content_hash": doc.content_hash,
                "source_path": doc.path,
                "embedder": embedder.name,
                "chunking": chunking.name,
            }
            if doc.id in stored:
                document_id = (
                    await conn.execute(
                        update(kb_documents)
                        .where(kb_documents.c.tenant_id == tenant_id, kb_documents.c.doc_id == doc.id)
                        .values(**values)
                        .returning(kb_documents.c.id)
                    )
                ).scalar_one()
                await conn.execute(delete(kb_chunks).where(kb_chunks.c.document_id == document_id))
                report.updated.append(doc.id)
            else:
                document_id = (
                    await conn.execute(
                        insert(kb_documents)
                        .values(tenant_id=tenant_id, doc_id=doc.id, **values)
                        .returning(kb_documents.c.id)
                    )
                ).scalar_one()
                report.added.append(doc.id)
            await conn.execute(
                insert(kb_chunks),
                [
                    {
                        "tenant_id": tenant_id,
                        "document_id": document_id,
                        "chunk_id": c.chunk_id,
                        "ordinal": c.ordinal,
                        "headings": list(c.headings),
                        "sections": list(c.sections),
                        "content": c.content,
                        "tokens": c.tokens,
                        "search_header": f"{doc.title} {' '.join(c.headings)}",
                        "embedding": v,
                    }
                    for c, v in zip(pieces, vectors, strict=True)
                ],
            )
        report.chunks += len(pieces)

    if prune:
        gone = sorted(set(stored) - {d.id for d in docs})
        if gone:
            async with engine.begin() as conn:
                await conn.execute(
                    delete(kb_documents).where(kb_documents.c.tenant_id == tenant_id, kb_documents.c.doc_id.in_(gone))
                )
        report.removed = gone
    return report
