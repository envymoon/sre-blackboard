"""Helpers for turning raw backend answers into small summaries.

Raw results never go into a prompt. A summary keeps what a diagnosis needs (counts, top-N,
extremes, the shape of a series, distinct error messages) and drops the rest; the evidence
id lets anyone look the full result up later.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.tools.base import canonical_json

MAX_SUMMARY_CHARS = 6000
MAX_STRING = 300


def num(value: Any, digits: int = 4) -> float | None:
    """A float rounded to ``digits`` significant digits; None for NaN, infinities and junk."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    if x == 0:
        return 0.0
    return round(x, max(0, digits - 1 - math.floor(math.log10(abs(x)))))


def clip(text: str, limit: int = MAX_STRING) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def iso(ts: float | datetime) -> str:
    """Seconds since the epoch (or a datetime) as an ISO timestamp, to the second."""
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, ``q`` in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


_TEMPLATE_RULES = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    # The demo's product ids (10 characters, at least one digit; plain words stay).
    (re.compile(r"\b(?=[A-Z0-9]*\d)[A-Z0-9]{10}\b"), "<id>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]


def template(message: str) -> str:
    """Collapse the variable parts of a log or error message, so repeats group together."""
    out = message
    for pattern, repl in _TEMPLATE_RULES:
        out = pattern.sub(repl, out)
    return clip(" ".join(out.split()), 200)


def _clip_strings(value: Any) -> Any:
    if isinstance(value, str):
        return clip(value)
    if isinstance(value, list):
        return [_clip_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _clip_strings(v) for k, v in value.items()}
    return value


def _lists(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[tuple[Any, ...], list[Any]]]:
    found: list[tuple[tuple[Any, ...], list[Any]]] = []
    if isinstance(value, list):
        found.append((path, value))
        for i, v in enumerate(value):
            found += _lists(v, (*path, i))
    elif isinstance(value, dict):
        for k, v in value.items():
            found += _lists(v, (*path, k))
    return found


def bound(summary: dict[str, Any], max_chars: int = MAX_SUMMARY_CHARS) -> tuple[dict[str, Any], bool]:
    """Fit a summary into ``max_chars`` of JSON by halving its largest lists (tails go first,
    so summarizers should put the most important items first). Returns (summary, truncated)."""
    out: dict[str, Any] = _clip_strings(summary)
    truncated = False
    while len(canonical_json(out)) > max_chars:
        candidates = [(len(canonical_json(lst)), lst) for _, lst in _lists(out) if len(lst) > 1]
        if not candidates:
            break
        _, largest = max(candidates, key=lambda c: c[0])
        del largest[max(1, len(largest) // 2) :]
        truncated = True
    if truncated:
        out["truncated"] = True
    return out, truncated
