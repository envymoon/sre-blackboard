"""logs_search: one service's logs from Loki, grouped into message templates.

The demo sends logs over OTLP, so a line is often just the message ("API request failed")
and the details (exception message, status codes, URL) are structured metadata. Loki only
returns that metadata when asked with the categorize-labels header (found the hard way in
the M2 lab, see docs/learn/m2-fault-lab.md).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field

from app.tools.base import Tool, ToolArgs
from app.tools.clients import Clients, get_json
from app.tools.compress import clip, iso, template

TOP = 10
CATEGORIZE = {"X-Loki-Response-Encoding-Flags": "categorize-labels"}
# Metadata worth showing next to an example line.
_USEFUL = re.compile(r"exception|error|status|code|url|path|rpc|http_|db_|peer|server_address", re.I)
_LEVELS = {
    "error": "error|fatal|critical|crit|emergency|alert",
    "warn": "warn|warning",
}
_RE2_SPECIAL = re.compile(r"([\\.+*?()|\[\]{}^$])")


class LogsSearchArgs(ToolArgs):
    service: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$", description="service_name, e.g. checkout")
    contains: str | None = Field(
        None, max_length=200, pattern=r"^[^`]*$", description="Case-insensitive text the line must contain"
    )
    level: Literal["error", "warn"] | None = Field(None, description="Only lines at this level or worse")
    minutes: int = Field(15, ge=1, le=360)
    limit: int = Field(200, ge=1, le=1000, description="Newest lines to fetch")
    end: datetime | None = Field(None, description="Window end (RFC 3339); default now")


def _re2_escape(text: str) -> str:
    return _RE2_SPECIAL.sub(r"\\\1", text)


def logql(args: LogsSearchArgs) -> str:
    q = f'{{service_name="{args.service}"}}'
    if args.contains:
        q += f" |~ `(?i){_re2_escape(args.contains)}`"
    if args.level:
        # Loki 3 sets detected_level; OTLP senders also carry severity_text. Either may be there.
        pattern = _LEVELS[args.level]
        q += f' | detected_level=~"(?i)({pattern})" or severity_text=~"(?i)({pattern})"'
    return q


async def _search(c: Clients, args: LogsSearchArgs, now: datetime) -> Any:
    end = args.end or now
    start = end - timedelta(minutes=args.minutes)
    params = {
        "query": logql(args),
        "start": int(start.timestamp() * 1e9),
        "end": int(end.timestamp() * 1e9),
        "limit": args.limit,
        "direction": "backward",
    }
    body = await get_json(c.loki, "/loki/api/v1/query_range", params=params, headers=CATEGORIZE)
    return {"query": params["query"], "result": body["data"]["result"]}


def _entries(raw: dict[str, Any]) -> list[tuple[int, str, dict[str, str], dict[str, str]]]:
    out = []
    for stream in raw["result"]:
        labels = stream.get("stream", {})
        for value in stream["values"]:
            ts, line = int(value[0]), value[1]
            extra = value[2] if len(value) > 2 else {}
            meta = {**extra.get("structuredMetadata", {}), **extra.get("parsed", {})}
            out.append((ts, line, meta, labels))
    out.sort(key=lambda e: e[0], reverse=True)
    return out


def _level(meta: dict[str, str], labels: dict[str, str]) -> str:
    for source in (meta, labels):
        for key in ("detected_level", "severity_text", "level"):
            if source.get(key):
                return source[key].lower()
    return "unknown"


def summarize(raw: dict[str, Any], args: LogsSearchArgs) -> dict[str, Any]:
    entries = _entries(raw)
    groups: dict[str, dict[str, Any]] = {}
    levels: Counter[str] = Counter()
    for ts, line, meta, labels in entries:
        level = _level(meta, labels)
        levels[level] += 1
        detail = meta.get("exception_message") or meta.get("exception.message") or ""
        key = template(f"{line} {detail}".strip())
        g = groups.get(key)
        if g is None:
            useful = {k: clip(v, 200) for k, v in sorted(meta.items()) if _USEFUL.search(k)}
            groups[key] = g = {
                "template": key,
                "count": 0,
                "level": level,
                "first_seen": ts,
                "last_seen": ts,
                "example": clip(line, 300),
                "attributes": dict(list(useful.items())[:8]),
            }
        g["count"] += 1
        g["first_seen"] = min(g["first_seen"], ts)
        g["last_seen"] = max(g["last_seen"], ts)
    top = sorted(groups.values(), key=lambda g: (-g["count"], g["template"]))[:TOP]
    for g in top:
        g["first_seen"] = iso(g["first_seen"] / 1e9)
        g["last_seen"] = iso(g["last_seen"] / 1e9)
    out: dict[str, Any] = {
        "query": raw["query"],
        "lines": len(entries),
        "hit_limit": len(entries) >= args.limit,
        "by_level": dict(sorted(levels.items())),
        "distinct_messages": len(groups),
        "groups": top,
    }
    if entries:
        out["newest"] = iso(entries[0][0] / 1e9)
        out["oldest"] = iso(entries[-1][0] / 1e9)
    else:
        out["note"] = "no matching lines in this window"
    return out


LOGS_SEARCH = Tool(
    name="logs_search",
    description=(
        "Search one service's logs (Loki). Returns line counts by level and the most frequent messages "
        "with an example and its attributes (exception message, status code, URL). Filter by text and level."
    ),
    args=LogsSearchArgs,
    backend="loki",
    fetch=_search,
    summarize=summarize,
    timeout_s=15.0,
)
