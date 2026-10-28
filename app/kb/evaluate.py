"""The retrieval evaluation and ablation (eval/retrieval/queries.yaml).

Every chunking preset is ingested into its own tenant, then every query is run in every
mode: ``bm25``, ``vector``, ``hybrid`` (RRF), ``rerank`` (RRF then the cross-encoder).

- HitRate@k: the share of answerable queries with a relevant section in the top k.
- MRR: the mean of 1 / rank of the first relevant result (0 past rank 10).
- Context tokens: the mean size of the top 3 results, what a prompt would pay for them.

The reranker's threshold is chosen on the dev split only, as the score that best separates
the answerable queries' best results from the unanswerable queries' best results (balanced
accuracy); the test split is reported with that threshold. Ranking is a separate question:
an answerable query whose top result is wrong still should not come back empty. The
reranker's top score for every query is in the report, so the cut can be checked by eye.

The metrics depend only on the corpus, the queries, the models and the code, so the report
is reproducible; CI recomputes it with the real models and fails when it no longer matches
the committed eval/retrieval/report.json (latency is reported but not compared).
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from app.kb.chunking import PRESETS, Document, chunk, tokens
from app.kb.models import Embedder, Reranker
from app.kb.search import MODES, Hit, KnowledgeBase, Mode
from app.kb.store import EMBED_TOKEN_LIMIT, ingest

KS = (1, 3, 5)
MRR_DEPTH = 10
QUERIES = Path("eval/retrieval/queries.yaml")
REPORT = Path("eval/retrieval/report.json")
TENANT_PREFIX = "kb-eval-"


@dataclass(frozen=True)
class Query:
    id: str
    kind: str
    query: str
    relevant: frozenset[str]

    @property
    def split(self) -> str:
        return "dev" if int(self.id.lstrip("q")) % 2 else "test"

    @property
    def answerable(self) -> bool:
        return bool(self.relevant)


def load_queries(path: Path = QUERIES) -> list[Query]:
    raw = yaml.safe_load(path.read_text())
    out = [Query(q["id"], q["kind"], q["query"], frozenset(q["relevant"])) for q in raw]
    if len({q.id for q in out}) != len(out):
        raise ValueError(f"{path}: duplicate query ids")
    return out


def check_labels(queries: Sequence[Query], docs: Sequence[Document]) -> list[str]:
    """Labels that name no section of the corpus (a renamed heading breaks them)."""
    known = {s for d in docs for c in chunk(d, PRESETS["section"]) for s in c.sections}
    return [f"{q.id}: {s}" for q in queries for s in sorted(q.relevant) if s not in known]


def first_relevant(hits: Sequence[Hit], relevant: frozenset[str]) -> int | None:
    for rank, h in enumerate(hits, 1):
        if relevant & set(h.sections):
            return rank
    return None


@dataclass
class Outcome:
    query: Query
    rank: int | None  # first relevant result, 1-based, within MRR_DEPTH
    top_score: float | None  # the best result's score (for the rerank mode: the reranker's)
    top_relevant: bool
    context_tokens: int  # the top 3 results' size
    latency_ms: float


def _metrics(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    positives = [o for o in outcomes if o.query.answerable]
    if not positives:
        return {"n": 0}
    m: dict[str, Any] = {"n": len(positives)}
    for k in KS:
        m[f"hit@{k}"] = round(sum(o.rank is not None and o.rank <= k for o in positives) / len(positives), 3)
    m["mrr"] = round(sum(1 / o.rank for o in positives if o.rank) / len(positives), 3)
    m["context_tokens"] = round(statistics.mean(o.context_tokens for o in positives))
    return m


def _thresholded(outcomes: Sequence[Outcome], threshold: float) -> dict[str, Any]:
    """With results below ``threshold`` dropped: answerable queries still answered with a
    relevant top result, and unanswerable ones correctly left empty."""
    pos = [o for o in outcomes if o.query.answerable]
    neg = [o for o in outcomes if not o.query.answerable]
    kept = [o for o in pos if o.top_score is not None and o.top_score >= threshold and o.top_relevant]
    lost = [o for o in pos if o.top_score is None or o.top_score < threshold]
    empty = [o for o in neg if o.top_score is None or o.top_score < threshold]
    return {
        "threshold": round(threshold, 4),
        "answered_relevant": round(len(kept) / len(pos), 3) if pos else None,
        "answerable_emptied": round(len(lost) / len(pos), 3) if pos else None,
        "unanswerable_empty": round(len(empty) / len(neg), 3) if neg else None,
    }


def choose_threshold(dev: Sequence[Outcome]) -> float:
    """The cut that best separates answerable from unanswerable queries on dev, by their best
    result's score. Ties go to the lowest such cut: dropping a useful passage costs more than
    showing a weak one, which the agent can ignore."""
    good = [o.top_score or 0.0 for o in dev if o.query.answerable]
    bad = [o.top_score or 0.0 for o in dev if not o.query.answerable]
    best, best_score = 0.0, -1.0
    for t in sorted({0.0, *good, *bad}):
        tpr = sum(s >= t for s in good) / len(good) if good else 0.0
        tnr = sum(s < t for s in bad) / len(bad) if bad else 0.0
        if (tpr + tnr) / 2 > best_score:
            best, best_score = t, (tpr + tnr) / 2
    # Halfway to the next lower score: the cut sits between the classes, not on a query.
    below = [s for s in (*good, *bad) if s < best]
    return (best + max(below)) / 2 if below else best


async def run(
    engine: AsyncEngine,
    docs: Sequence[Document],
    queries: Sequence[Query],
    embedder: Embedder,
    reranker: Reranker,
    *,
    chunkings: Sequence[str] = tuple(PRESETS),
    modes: Sequence[Mode] = MODES,
) -> dict[str, Any]:
    problems = check_labels(queries, docs)
    if problems:
        raise ValueError("labels that name no section: " + "; ".join(problems))
    report: dict[str, Any] = {
        "models": {"embedder": embedder.name, "reranker": reranker.name},
        "corpus": {"documents": len(docs)},
        "queries": {
            "answerable": sum(q.answerable for q in queries),
            "unanswerable": sum(not q.answerable for q in queries),
            "dev": sum(q.split == "dev" for q in queries),
            "test": sum(q.split == "test" for q in queries),
        },
        "results": [],
        "latency_ms": {},
    }
    for name in chunkings:
        tenant = TENANT_PREFIX + name
        await ingest(engine, tenant, docs, embedder, chunking=PRESETS[name], prune=True)
        chunks = [c for d in docs for c in chunk(d, PRESETS[name])]
        report["corpus"][f"chunks_{name}"] = len(chunks)
        report["corpus"][f"over_embed_limit_{name}"] = sum(c.tokens > EMBED_TOKEN_LIMIT for c in chunks)
        # min_score=0: every candidate is kept, the threshold is applied afterwards.
        kb = KnowledgeBase(engine, embedder, reranker, min_score=0.0)
        for mode in modes:
            outcomes = [await _one(kb, tenant, q, mode) for q in queries]
            entry: dict[str, Any] = {
                "chunking": name,
                "mode": mode,
                "all": _metrics(outcomes),
                "test": _metrics([o for o in outcomes if o.query.split == "test"]),
                "by_kind": {
                    kind: _metrics([o for o in outcomes if o.query.kind == kind])
                    for kind in sorted({q.kind for q in queries if q.answerable})
                },
                "misses_at_5": sorted(
                    o.query.id for o in outcomes if o.query.answerable and not (o.rank and o.rank <= 5)
                ),
            }
            if mode == "rerank":
                t = choose_threshold([o for o in outcomes if o.query.split == "dev"])
                entry["threshold"] = {
                    "dev": _thresholded([o for o in outcomes if o.query.split == "dev"], t),
                    "test": _thresholded([o for o in outcomes if o.query.split == "test"], t),
                }
                entry["top_scores"] = {o.query.id: round(o.top_score or 0.0, 3) for o in outcomes}
            report["results"].append(entry)
            lat = sorted(o.latency_ms for o in outcomes)
            report["latency_ms"][f"{name}/{mode}"] = {
                "p50": round(lat[len(lat) // 2], 1),
                "p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
            }
    return report


async def _one(kb: KnowledgeBase, tenant: str, q: Query, mode: Mode) -> Outcome:
    started = time.perf_counter()
    result = await kb.search(tenant, q.query, k=MRR_DEPTH, mode=mode)
    latency = (time.perf_counter() - started) * 1000
    hits = result.hits
    rank = first_relevant(hits, q.relevant)
    return Outcome(
        query=q,
        rank=rank,
        top_score=hits[0].score if hits else None,
        top_relevant=rank == 1,
        context_tokens=sum(tokens(h.content) for h in hits[:3]),
        latency_ms=latency,
    )


def github_annotations(report: dict[str, Any], part: int = 3000) -> list[str]:
    """GitHub ``::notice`` lines: a table per chunking, then the whole report in parts (job
    logs and artifacts may be out of reach; annotations can be read through the API)."""
    lines = []
    for name in sorted({r["chunking"] for r in report["results"]}):
        rows = [
            f"{r['mode']}: hit@1 {r['all']['hit@1']} hit@3 {r['all']['hit@3']} hit@5 {r['all']['hit@5']} "
            f"mrr {r['all']['mrr']} tokens {r['all']['context_tokens']}"
            for r in report["results"]
            if r["chunking"] == name
        ]
        lines.append(f"::notice title=retrieval {name}::" + "%0A".join(rows))
    text = json.dumps(report, separators=(",", ":")).replace("%", "%25")
    pieces = [text[i : i + part] for i in range(0, len(text), part)]
    lines += [f"::notice title=report {i}/{len(pieces)}::{p}" for i, p in enumerate(pieces, 1)]
    return lines


def comparable(report: dict[str, Any]) -> dict[str, Any]:
    """The parts of a report that must reproduce exactly (everything but latency)."""
    return {k: v for k, v in report.items() if k != "latency_ms"}


def differences(expected: Any, got: Any, path: str = "", tolerance: float = 0.02) -> list[str]:
    """Where two reports disagree; numbers may differ by ``tolerance`` (CPU float noise can
    swap two near-equal scores)."""
    if isinstance(expected, dict) and isinstance(got, dict):
        out = []
        for k in sorted(set(expected) | set(got)):
            if k not in expected or k not in got:
                out.append(f"{path}/{k}: only in {'the new' if k in got else 'the committed'} report")
            else:
                out += differences(expected[k], got[k], f"{path}/{k}", tolerance)
        return out
    numbers = isinstance(expected, int | float) and isinstance(got, int | float)
    if numbers and (isinstance(expected, float) or isinstance(got, float)) and abs(expected - got) <= tolerance:
        return []
    return [] if expected == got else [f"{path}: {expected!r} -> {got!r}"]
