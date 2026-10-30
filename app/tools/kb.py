"""kb_search: the runbooks and postmortems most relevant to a question (M6, ADR-0010).

A read-only tool like the others: its result is stored as evidence and gets an id. What it
returns is background (what usually goes wrong, what to check), not a measurement of this
incident, so the citation check accepts a knowledge result only next to live evidence
(app/agents/citations.py). The search is always inside the incident's tenant: the tenant
comes from the tool context, never from the model's arguments.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import Field

from app.tools.base import Tool, ToolArgs, ToolError
from app.tools.clients import Clients

MAX_CHARS_PER_HIT = 900


class KbSearchArgs(ToolArgs):
    query: str = Field(
        min_length=3,
        max_length=300,
        description="What you want to know, in words or with the exact error text, e.g. "
        "'checkout fails with connection refused to cart' or 'Payment request failed. Invalid token.'",
    )
    k: int = Field(3, ge=1, le=5, description="How many passages")


class Searcher(Protocol):
    async def search_for_tool(self, tenant_id: str, query: str, k: int) -> dict[str, Any]: ...


async def _search(c: Clients, args: KbSearchArgs, now: datetime) -> Any:
    kb: Searcher | None = c.extra.get("kb")
    if kb is None:
        raise ToolError("unavailable", "the knowledge base is not configured on this worker")
    if not c.tenant_id:
        raise ToolError("forbidden", "a knowledge search needs the incident's tenant")
    return await kb.search_for_tool(c.tenant_id, args.query, args.k)


def _lines(content: str) -> list[str]:
    """The passage as lines (the summary bound clips any one string to 300 characters), at
    most MAX_CHARS_PER_HIT in all."""
    out: list[str] = []
    used = 0
    for line in (ln.strip() for ln in content.splitlines()):
        if not line:
            continue
        if used + len(line) > MAX_CHARS_PER_HIT:
            out.append(line[: max(0, MAX_CHARS_PER_HIT - used)] + "…")
            break
        out.append(line)
        used += len(line)
    return out


def summarize(raw: dict[str, Any], args: KbSearchArgs) -> dict[str, Any]:
    hits = raw["hits"]
    out: dict[str, Any] = {
        "status": raw["status"],
        "note": "Background from runbooks and past incidents: what usually goes wrong and what to check. "
        "It is not data about this incident; confirm with live queries before relying on it.",
        "passages": [
            {
                "source": f"{h['doc_id']} ({h['kind']}, version {h['version']})",
                "section": " / ".join(h["headings"]),
                "relevance": round(h["score"], 3),
                "text": _lines(h["content"]),
            }
            for h in hits
        ],
    }
    if not hits:
        out["note"] = "Nothing in the knowledge base is relevant enough. Rely on live data."
    if raw.get("degraded"):
        out["degraded"] = "the reranker was unavailable; passages are in their fused order, unfiltered"
    return out


KB_SEARCH = Tool(
    name="kb_search",
    description="Search the team's runbooks and postmortems. Returns background on how a failure "
    "usually shows up and what to check; not evidence about this incident on its own.",
    args=KbSearchArgs,
    backend="kb",
    fetch=_search,
    summarize=summarize,
    timeout_s=30.0,  # the first call loads the models
)
