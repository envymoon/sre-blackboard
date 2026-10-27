"""One incident as the console sees it, frozen into a file.

``python -m app.console.export <incident-id> -o case.json`` asks the API (in-process, as a
viewer of the incident's tenant) for every response the incident page reads, and writes them
keyed by path: the list, the incident, its diagnosis, its call tree, its plans, its audit
trail and every piece of evidence a tool call produced. A raw tool result over
``max_result_bytes`` is left out (a trace search can be megabytes); its summary, which is what
the model read, its size and its hash stay, so the page still shows and proves it. The static site serves the page from
that file instead of a server, so the replay shows exactly what the API returned, nothing
re-typed by hand. The page replays it by the stored timestamps.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from app.api.app import create_app
from app.core.config import Settings, get_settings
from app.core.security import mint_token
from app.db import incidents

FORMAT = 1
MAX_RESULT_BYTES = 64 * 1024


async def export(settings: Settings, incident_id: UUID, *, max_result_bytes: int = MAX_RESULT_BYTES) -> dict[str, Any]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://export") as client,
    ):
        async with app.state.engine.connect() as conn:
            row = await incidents.get(conn, incident_id, tenant_id=None)
        if row is None:
            raise LookupError(f"incident {incident_id} not found")
        token = mint_token(settings, subject="console-export", tenant_id=row["tenant_id"], roles=["viewer"])
        headers = {"Authorization": f"Bearer {token}"}

        async def get(path: str) -> Any:
            resp = await client.get(path, headers=headers)
            resp.raise_for_status()
            return resp.json()

        base = f"/api/v1/incidents/{incident_id}"
        responses: dict[str, Any] = {}
        incident = await get(base)
        # The list as it stands, narrowed to this incident: the replay starts it from empty.
        responses["/api/v1/incidents"] = [incident]
        responses[base] = incident
        for part in ("diagnosis", "agents", "plans", "audit"):
            responses[f"{base}/{part}"] = await get(f"{base}/{part}")
        cited = sorted(
            {t["evidence_id"] for run in responses[f"{base}/agents"] for s in run["llm_calls"] for t in s["tools"]}
            - {None}
        )
        for ev in cited:
            found = await get(f"/api/v1/evidence/{ev}")
            if found["result_bytes"] > max_result_bytes:
                found = {**found, "result": None, "result_omitted": True}
            responses[f"/api/v1/evidence/{ev}"] = found
    return {
        "format": FORMAT,
        "incident_id": str(incident_id),
        "exported_at": datetime.now(UTC).isoformat(),
        "responses": responses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.console.export", description=__doc__.split("\n\n")[0])
    parser.add_argument("incident", type=UUID)
    parser.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    parser.add_argument(
        "--max-result-bytes", type=int, default=MAX_RESULT_BYTES, help="leave out raw tool results larger than this"
    )
    args = parser.parse_args()
    try:
        doc = asyncio.run(export(get_settings(), args.incident, max_result_bytes=args.max_result_bytes))
    except LookupError as e:
        sys.exit(str(e))
    text = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
