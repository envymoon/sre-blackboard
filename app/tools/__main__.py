"""Call the read-only tools by hand.

    python -m app.tools list
    python -m app.tools call prom_query '{"query": "service:sli_error_ratio:rate5m"}'
    python -m app.tools call rollout_history '{"deployment": "checkout"}' --record lab.jsonl
    python -m app.tools call rollout_history '{"deployment": "checkout"}' --replay lab.jsonl

Prints what a model would see (summary and evidence id, or a structured error). Endpoints
come from SRE_* settings, .env and .lab.env (`make lab-env` writes the lab's).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.config import get_settings
from app.tools import ALL_TOOLS, ToolContext, ToolRegistry
from app.tools.clients import Clients
from app.tools.evidence import MemoryEvidenceStore
from app.tools.sources import Cassette, LiveSource, RecordingSource, ReplaySource, Source


async def _call(args: argparse.Namespace) -> int:
    clients: Clients | None = None
    cassette: Cassette | None = None
    source: Source
    if args.replay:
        source = ReplaySource(Cassette.load(Path(args.replay)))
    else:
        clients = Clients.from_settings(get_settings())
        source = LiveSource(clients)
        if args.record:
            path = Path(args.record)
            cassette = Cassette.load(path) if path.exists() else Cassette(meta={"recorded_by": "app.tools"})
            source = RecordingSource(source, cassette)
    store = MemoryEvidenceStore()
    registry = ToolRegistry(ALL_TOOLS, source, store)
    try:
        outcome = await registry.call(args.tool, json.loads(args.args), ToolContext(tenant_id=args.tenant))
    finally:
        if clients is not None:
            await clients.aclose()
        if cassette is not None:
            cassette.dump(Path(args.record))
    print(json.dumps(json.loads(outcome.for_model()), indent=2, ensure_ascii=False))
    if args.verbose:
        print(f"attempts={outcome.attempts} latency_ms={outcome.latency_ms} {outcome.meta}", file=sys.stderr)
    return 0 if outcome.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tools", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="tool definitions as given to the model")
    p = sub.add_parser("call", help="call one tool")
    p.add_argument("tool")
    p.add_argument("args", nargs="?", default="{}", help="arguments as JSON")
    p.add_argument("--tenant", default="lab")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--record", metavar="CASSETTE", help="call live and append the answer to this cassette")
    group.add_argument("--replay", metavar="CASSETTE", help="answer from this cassette instead of the backends")
    p.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "list":
        print(json.dumps([t.spec() for t in ALL_TOOLS], indent=2))
        return 0
    return asyncio.run(_call(args))


if __name__ == "__main__":
    sys.exit(main())
