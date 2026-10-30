"""M6 knowledge base: chunking, ingest, hybrid search and its tenant boundary, the evaluation.

The models here are the deterministic stand-ins (hash embedder, overlap reranker): these
tests check the plumbing, not retrieval quality. Quality is the retrieval evaluation's job,
with the real models, in CI (eval/retrieval/report.json).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.schema import kb_chunks, kb_documents
from app.kb import evaluate
from app.kb.chunking import DEFAULT_CHUNKING, PRESETS, ChunkParams, chunk, load_dir, parse, redact, slug
from app.kb.models import DEFAULT_EMBEDDER, DEFAULT_RERANKER, DIM, HashEmbedder, OverlapReranker
from app.kb.search import Hit, KnowledgeBase, or_query, rrf
from app.kb.store import ingest
from app.tools import ALL_TOOLS, ToolContext, ToolRegistry
from app.tools.clients import Clients
from app.tools.evidence import MemoryEvidenceStore
from app.tools.sources import Cassette, LiveSource, RecordingSource, ReplaySource

CORPUS = load_dir(Path("kb"))

DOC = """---
id: RB-TEST
kind: runbook
title: Test service
services: [checkout]
version: "1"
---
Intro line.

## First checks

Look at the payment hop first.

## Rolling back

Use rollout_undo after a bad release.
"""


def _hit(chunk_id: str) -> Hit:
    return Hit(chunk_id, chunk_id.split("#")[0], "runbook", "t", "1", [], [], "", [])


# --- chunking ---------------------------------------------------------------------------


def test_a_document_needs_its_front_matter() -> None:
    with pytest.raises(ValueError, match="front matter"):
        parse("# no front matter\n")
    with pytest.raises(ValueError, match="lacks services"):
        parse("---\nid: RB-X\nkind: runbook\ntitle: x\nversion: '1'\n---\nbody\n")
    with pytest.raises(ValueError, match="kind must be"):
        parse(DOC.replace("kind: runbook", "kind: wiki"))


def test_sections_become_chunks_with_stable_labels() -> None:
    chunks = chunk(parse(DOC), PRESETS["section"])
    assert [c.headings for c in chunks] == [("Overview",), ("First checks",), ("Rolling back",)]
    assert chunks[1].sections == ("RB-TEST#first-checks",)
    assert chunks[1].chunk_id == "RB-TEST#02"
    assert chunks[1].text_for_embedding("Test service").startswith("Test service > First checks\n")
    # The original parameters merge sections under 200 tokens into the chunk before them.
    merged = chunk(parse(DOC), PRESETS["original"])
    assert len(merged) == 1
    assert merged[0].sections == ("RB-TEST#overview", "RB-TEST#first-checks", "RB-TEST#rolling-back")


def test_a_long_section_is_split_with_overlap_and_code_stays_whole() -> None:
    para = " ".join(f"word{i}" for i in range(120))
    code = "```\n" + "\n".join(f"line {i}" for i in range(40)) + "\n```"
    body = DOC.split("## First")[0] + "## Long\n\n" + "\n\n".join([para, code, para, para, para, para])
    small = ChunkParams("small", target=150, min=0, max=200, overlap=20)
    pieces = [c for c in chunk(parse(body), small) if c.headings == ("Long",)]
    assert len(pieces) > 2
    assert all(c.content.count("```") in (0, 2) for c in pieces)
    # Each piece after the first starts with the end of the one before it.
    assert pieces[1].content.split()[0] in pieces[0].content


def test_secrets_are_redacted_before_indexing() -> None:
    assert "sk-abcdefgh1234" not in redact("key sk-abcdefgh1234 here")
    assert redact("AKIAABCDEFGHIJKLMNOP") == "[REDACTED]"
    assert slug("Telling checkout's fault") == "telling-checkout-s-fault"


def test_the_corpus_parses_and_every_label_names_a_section() -> None:
    assert len(CORPUS) >= 20
    assert {d.kind for d in CORPUS} == {"runbook", "postmortem"}
    queries = evaluate.load_queries()
    assert len(queries) >= 50
    assert evaluate.check_labels(queries, CORPUS) == []
    assert {q.split for q in queries} == {"dev", "test"}


# --- ingest -----------------------------------------------------------------------------


async def _count(engine, table, tenant: str) -> int:  # type: ignore[no-untyped-def]
    async with engine.connect() as conn:
        return (
            await conn.execute(select(func.count()).select_from(table).where(table.c.tenant_id == tenant))
        ).scalar_one()


async def test_ingest_is_idempotent_and_replaces_a_changed_document(engine) -> None:  # type: ignore[no-untyped-def]
    docs = [parse(DOC)]
    first = await ingest(engine, "t1", docs, HashEmbedder())
    assert first.added == ["RB-TEST"] and first.chunks == 3
    again = await ingest(engine, "t1", docs, HashEmbedder())
    assert again.unchanged == ["RB-TEST"] and again.chunks == 0
    changed = [parse(DOC.replace("payment hop", "cart hop"))]
    third = await ingest(engine, "t1", changed, HashEmbedder())
    assert third.updated == ["RB-TEST"]
    assert await _count(engine, kb_chunks, "t1") == 3
    # A different chunking re-indexes the document, like a different model would.
    fourth = await ingest(engine, "t1", changed, HashEmbedder(), chunking=PRESETS["original"])
    assert fourth.updated == ["RB-TEST"] and await _count(engine, kb_chunks, "t1") == 1
    pruned = await ingest(engine, "t1", [], HashEmbedder(), prune=True)
    assert pruned.removed == ["RB-TEST"]
    assert await _count(engine, kb_documents, "t1") == 0 and await _count(engine, kb_chunks, "t1") == 0


# --- search -----------------------------------------------------------------------------


def test_rrf_rewards_agreement_over_one_lists_top_rank() -> None:
    a = [_hit("A#01"), _hit("B#01"), _hit("C#01")]
    b = [_hit("B#01"), _hit("D#01"), _hit("A#01")]
    fused = rrf(a, b)
    assert [h.chunk_id for h in fused][:2] == ["B#01", "A#01"]  # 1/61+1/62 > 1/61+1/63
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert or_query("Why is checkout's p95 > 1000 ms?") == "1000 | checkout | is | ms | p95 | s | why"
    assert or_query("?!") is None


async def test_search_never_crosses_a_tenant(engine) -> None:  # type: ignore[no-untyped-def]
    await ingest(engine, "tenant-a", [parse(DOC)], HashEmbedder())
    other = parse(DOC.replace("payment hop", "secret of tenant b"))
    await ingest(engine, "tenant-b", [other], HashEmbedder())
    kb = KnowledgeBase(engine, HashEmbedder(), OverlapReranker(), min_score=0.0)
    for mode in ("bm25", "vector", "hybrid", "rerank"):
        a = await kb.search("tenant-a", "secret of tenant b", mode=mode)  # type: ignore[arg-type]
        assert all("secret" not in h.content for h in a.hits), mode
        none = await kb.search("tenant-c", "payment hop", mode=mode)  # type: ignore[arg-type]
        assert none.hits == [] and none.status == "no_relevant_knowledge"
    with pytest.raises(ValueError, match="tenant"):
        await kb.search("", "payment")


async def test_search_modes_threshold_and_degraded_reranker(engine) -> None:  # type: ignore[no-untyped-def]
    await ingest(engine, "lab", CORPUS, HashEmbedder())
    kb = KnowledgeBase(engine, HashEmbedder(), OverlapReranker(), min_score=0.5)
    exact = await kb.search("lab", "Payment request failed. Invalid token.", mode="bm25")
    assert exact.hits and exact.hits[0].ranks == {"bm25": 1}
    full = await kb.search("lab", "Payment request failed. Invalid token.")
    assert full.status == "ok" and full.mode == "rerank"
    assert all(h.score >= 0.5 for h in full.hits)
    assert any("bm25" in h.ranks and "vector" in h.ranks for h in full.hits)
    # A query sharing no words with the corpus is dropped by the threshold: an answer, not an error.
    empty = await kb.search("lab", "zzyzx qwertyuiop")
    assert empty.hits == [] and not empty.degraded
    scoped = await kb.search("lab", "rolling back a release", mode="hybrid", services=["payment"])
    assert scoped.hits and all("payment" in h.services for h in scoped.hits)

    class Broken:
        name = "broken"

        def scores(self, query: str, passages: Sequence[str]) -> list[float]:
            raise RuntimeError("onnx session died")

    degraded = await KnowledgeBase(engine, HashEmbedder(), Broken()).search("lab", "payment failure flag", k=3)
    assert degraded.degraded and len(degraded.hits) == 3


async def test_the_vector_path_ignores_chunks_from_another_embedder(engine) -> None:  # type: ignore[no-untyped-def]
    await ingest(engine, "lab", [parse(DOC)], HashEmbedder())

    class Other(HashEmbedder):
        name = "other-model"

    assert (await KnowledgeBase(engine, Other()).search("lab", "payment hop", mode="vector")).hits == []
    assert len(HashEmbedder().query("x")) == DIM


# --- the evaluation ---------------------------------------------------------------------


async def test_the_evaluation_runs_every_mode_and_chunking(engine) -> None:  # type: ignore[no-untyped-def]
    queries = evaluate.load_queries()
    report = await evaluate.run(engine, CORPUS, queries, HashEmbedder(), OverlapReranker())
    configs = {(r["chunking"], r["mode"]) for r in report["results"]}
    assert configs == {(c, m) for c in ("original", "section") for m in ("bm25", "vector", "hybrid", "rerank")}
    for r in report["results"]:
        assert 0 <= r["all"]["hit@1"] <= r["all"]["hit@3"] <= r["all"]["hit@5"] <= 1
        assert r["all"]["n"] == report["queries"]["answerable"]
    rerank = next(r for r in report["results"] if r["mode"] == "rerank")
    assert set(rerank["threshold"]) == {"dev", "test"}
    assert evaluate.differences(evaluate.comparable(report), evaluate.comparable(report)) == []


def test_threshold_choice_and_report_comparison() -> None:
    def outcome(qid: str, relevant: bool, score: float, answerable: bool = True) -> evaluate.Outcome:
        q = evaluate.Query(qid, "agent", "q", frozenset({"X#a"}) if answerable else frozenset())
        return evaluate.Outcome(q, 1 if relevant else None, score, relevant, 0, 0.0)

    dev = [
        outcome("q1", True, 0.9),
        outcome("q3", False, 0.6),  # answerable, top result wrong: still should not be emptied
        outcome("q5", False, 0.2, answerable=False),
        outcome("q7", False, 0.1, answerable=False),
    ]
    assert evaluate.choose_threshold(dev) == pytest.approx(0.4)  # between 0.2 and 0.6
    assert evaluate.differences({"a": {"hit@1": 0.5, "n": 3}}, {"a": {"hit@1": 0.51, "n": 3}}) == []
    assert evaluate.differences({"a": {"hit@1": 0.5}}, {"a": {"hit@1": 0.6}}) == ["/a/hit@1: 0.5 -> 0.6"]
    assert evaluate.differences({"misses": ["q1"]}, {"misses": ["q2"]}) == ["/misses: ['q1'] -> ['q2']"]


# --- the kb_search tool -----------------------------------------------------------------


class FakeSearcher:
    def __init__(self) -> None:
        self.tenants: list[str] = []

    async def search_for_tool(self, tenant_id: str, query: str, k: int) -> dict[str, Any]:
        self.tenants.append(tenant_id)
        hit = {
            "chunk_id": "RB-PAYMENT#02",
            "doc_id": "RB-PAYMENT",
            "kind": "runbook",
            "title": "payment service",
            "version": "2026-10-28",
            "headings": ["Typical failures"],
            "score": 0.97,
            "content": "## Typical failures\n\n" + "\n".join(["y" * 250] * 8),
        }
        return {"status": "ok", "degraded": False, "hits": [hit][:k]}


async def test_kb_search_runs_in_the_incidents_tenant_and_replays() -> None:
    searcher = FakeSearcher()
    clients = Clients(*(httpx.AsyncClient() for _ in range(5)), extra={"kb": searcher})
    cassette = Cassette()
    registry = ToolRegistry(ALL_TOOLS, RecordingSource(LiveSource(clients), cassette), MemoryEvidenceStore())
    out = await registry.call("kb_search", {"query": "payment declines", "k": 2}, ToolContext(tenant_id="lab-rag"))
    assert out.ok and searcher.tenants == ["lab-rag"]
    assert (
        out.summary is not None and out.summary["passages"][0]["source"] == "RB-PAYMENT (runbook, version 2026-10-28)"
    )
    lines = out.summary["passages"][0]["text"]
    assert lines[0] == "## Typical failures" and sum(map(len, lines)) <= 901 and lines[-1].endswith("…")
    assert "not data about this incident" in out.summary["note"]
    # The model cannot pick a tenant: unknown arguments are refused.
    bad = await registry.call("kb_search", {"query": "payment", "tenant": "other"}, ToolContext(tenant_id="lab"))
    assert bad.error is not None and bad.error.code == "invalid_args"
    replayed = await ToolRegistry(ALL_TOOLS, ReplaySource(cassette), MemoryEvidenceStore()).call(
        "kb_search", {"query": "payment declines", "k": 2}, ToolContext(tenant_id="lab-rag")
    )
    assert replayed.summary == out.summary and searcher.tenants == ["lab-rag"]
    unconfigured = Clients(*(httpx.AsyncClient() for _ in range(5)))
    none = await ToolRegistry(ALL_TOOLS, LiveSource(unconfigured), MemoryEvidenceStore()).call(
        "kb_search", {"query": "payment"}, ToolContext(tenant_id="lab")
    )
    assert none.error is not None and none.error.code == "unavailable"
    await clients.aclose()
    await unconfigured.aclose()


def test_the_committed_report_and_the_defaults_agree() -> None:
    """The default threshold is the one the evaluation chose, for the default chunking."""
    report = json.loads(evaluate.REPORT.read_text())
    assert report["models"] == {"embedder": DEFAULT_EMBEDDER, "reranker": DEFAULT_RERANKER}
    chosen = next(r for r in report["results"] if r["chunking"] == DEFAULT_CHUNKING and r["mode"] == "rerank")
    assert Settings(_env_file=None).kb_min_score == pytest.approx(chosen["threshold"]["dev"]["threshold"], abs=0.005)  # type: ignore[call-arg]
    assert report["queries"]["answerable"] + report["queries"]["unanswerable"] == len(evaluate.load_queries())
