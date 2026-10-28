"""The knowledge base by hand: download the models, ingest, search, evaluate.

    python -m app.kb models                       # fetch the weights into SRE_KB_MODEL_DIR
    python -m app.kb ingest --tenant lab          # kb/ -> kb_documents, kb_chunks (idempotent)
    python -m app.kb search --tenant lab "checkout errors after a release" [--mode bm25]
    python -m app.kb eval [--check] [--annotate]  # the ablation; --check: compare with the committed report

The database is SRE_DATABASE_URL. ``--embedder hash --reranker overlap`` uses the
deterministic stand-ins (no download; not meaningful for quality).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.kb import evaluate
from app.kb.chunking import DEFAULT_CHUNKING, PRESETS, load_dir
from app.kb.models import Embedder, Reranker, download, embedder, reranker
from app.kb.search import MODES, KnowledgeBase
from app.kb.store import ingest


def _models(settings: Settings, args: argparse.Namespace) -> tuple[Embedder, Reranker]:
    cache, offline = settings.kb_model_dir, settings.kb_offline
    return (
        embedder(args.embedder or settings.kb_embedder, cache, offline),
        reranker(args.reranker or settings.kb_reranker, cache, offline),
    )


async def _ingest(settings: Settings, args: argparse.Namespace) -> int:
    emb = embedder(args.embedder or settings.kb_embedder, settings.kb_model_dir, settings.kb_offline)
    docs = load_dir(Path(args.dir or settings.kb_dir))
    engine = create_async_engine(settings.database_url)
    try:
        tenants = list(args.tenant or [])
        if args.all_tenants:
            tenants += [t for t in [*settings.kb_tenants, *settings.eval_tenant_variants] if t not in tenants]
        if not tenants:
            raise SystemExit("ingest: name --tenant, or --all-tenants")
        for tenant in tenants:
            report = await ingest(engine, tenant, docs, emb, chunking=PRESETS[args.chunking], prune=args.prune)
            print(json.dumps({"tenant": tenant, **report.to_dict()}))
    finally:
        await engine.dispose()
    return 0


async def _search(settings: Settings, args: argparse.Namespace) -> int:
    emb, rr = _models(settings, args)
    engine = create_async_engine(settings.database_url)
    try:
        kb = KnowledgeBase(engine, emb, rr, min_score=settings.kb_min_score)
        result = await kb.search(args.tenant, args.query, k=args.k, mode=args.mode)
    finally:
        await engine.dispose()
    print(f"{result.status} ({result.mode}{', reranker degraded' if result.degraded else ''})")
    for h in result.hits:
        print(f"{h.score:8.4f}  {h.chunk_id:40} {' / '.join(h.headings)}  {h.ranks}")
    return 0


async def _eval(settings: Settings, args: argparse.Namespace) -> int:
    emb, rr = _models(settings, args)
    docs = load_dir(Path(args.dir or settings.kb_dir))
    queries = evaluate.load_queries(Path(args.queries))
    engine = create_async_engine(settings.database_url)
    try:
        report = await evaluate.run(engine, docs, queries, emb, rr)
    finally:
        await engine.dispose()
    out = Path(args.out)
    if args.annotate:
        for line in evaluate.github_annotations(report):
            print(line)
    if args.check:
        committed = json.loads(out.read_text())
        diff = evaluate.differences(evaluate.comparable(committed), evaluate.comparable(report))
        print(json.dumps(report, indent=1))
        if diff:
            print(f"{out} does not match this run (update it with `python -m app.kb eval`):", file=sys.stderr)
            print("\n".join(diff[:40]), file=sys.stderr)
            return 1
        print(f"{out} reproduces", file=sys.stderr)
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps(report, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.kb", description=__doc__.split("\n")[0])
    parser.add_argument("--embedder", help="override SRE_KB_EMBEDDER ('hash': the test stand-in)")
    parser.add_argument("--reranker", help="override SRE_KB_RERANKER ('overlap': the test stand-in)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models", help="download the embedder and reranker weights")
    p = sub.add_parser("ingest", help="chunk, embed and store the knowledge base for tenants")
    p.add_argument("--tenant", action="append", help="repeat for several tenants")
    p.add_argument("--all-tenants", action="store_true", help="SRE_KB_TENANTS and every evaluation tenant")
    p.add_argument("--dir", help="documents (default: SRE_KB_DIR)")
    p.add_argument("--chunking", choices=sorted(PRESETS), default=DEFAULT_CHUNKING)
    p.add_argument("--prune", action="store_true", help="delete documents no longer in the directory")
    p = sub.add_parser("search", help="one query, with each result's score and per-path ranks")
    p.add_argument("--tenant", required=True)
    p.add_argument("query")
    p.add_argument("--mode", choices=MODES, default="rerank")
    p.add_argument("-k", type=int, default=5)
    p = sub.add_parser("eval", help="the retrieval evaluation and ablation")
    p.add_argument("--dir", help="documents (default: SRE_KB_DIR)")
    p.add_argument("--queries", default=str(evaluate.QUERIES))
    p.add_argument("--out", default=str(evaluate.REPORT))
    p.add_argument("--check", action="store_true", help="compare with --out instead of writing it")
    p.add_argument("--annotate", action="store_true", help="also print the results as GitHub annotations")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.cmd == "models":
        download(args.embedder or settings.kb_embedder, args.reranker or settings.kb_reranker, settings.kb_model_dir)
        return 0
    run = {"ingest": _ingest, "search": _search, "eval": _eval}[args.cmd]
    return asyncio.run(run(settings, args))


if __name__ == "__main__":
    sys.exit(main())
