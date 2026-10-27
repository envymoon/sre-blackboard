"""What a tool is, what a call returns, and the errors a model can act on.

A tool has two halves. ``fetch`` talks to a backend (Prometheus, Loki, Jaeger, the Kubernetes
API, flagd) and returns the raw answer as plain JSON data. ``summarize`` turns that raw answer
into a small, bounded summary. The raw answer is stored as evidence; only the summary and the
evidence id go into the prompt. Keeping the two apart is also what makes record/replay work:
a cassette stores raw answers, and summarizing them again gives the same bytes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from app.tools.clients import Clients

Json = Any
RiskTier = Literal["R0", "R1", "R2", "R3"]


class ToolArgs(BaseModel):
    """Base for every tool's arguments: unknown fields are an error, not silently dropped."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolError(Exception):
    """A failure the calling model can read and react to (fix the query, try another source).

    ``code`` is one of: invalid_args, bad_query, not_found, forbidden, timeout, unavailable,
    backend_error, too_large, circuit_open, replay_miss, unknown_tool.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.attempts = 1

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


Fetch = Callable[["Clients", Any, datetime], Awaitable[Json]]
Summarize = Callable[[Json, Any], dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[ToolArgs]
    backend: str
    fetch: Fetch
    summarize: Summarize
    risk: RiskTier = "R0"
    timeout_s: float = 10.0
    max_result_bytes: int = 4_000_000

    def spec(self) -> dict[str, Any]:
        """The definition handed to the model (Anthropic tool-use shape)."""
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    incident_id: UUID | None = None
    agent: str | None = None
    # What evidence ids are unique within; defaults to the incident. A replayed evaluation
    # run sets it to something stable across runs (e.g. the scenario name).
    scope: str | None = None

    @property
    def evidence_scope(self) -> str:
        return self.scope or f"{self.tenant_id}/{self.incident_id or 'adhoc'}"


@dataclass
class ToolOutcome:
    tool: str
    args: dict[str, Any]
    evidence_id: str | None = None
    summary: dict[str, Any] | None = None
    error: ToolError | None = None
    attempts: int = 1
    latency_ms: int = 0
    fetched_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    def for_model(self) -> str:
        """What goes back into the transcript: the summary with its evidence id, or the error."""
        body: dict[str, Any]
        if self.error is not None:
            body = {"tool": self.tool, "error": self.error.to_dict()}
        else:
            body = {"tool": self.tool, "evidence_id": self.evidence_id, "summary": self.summary}
        return canonical_json(body)


def without_nul(value: Any) -> Any:
    """``value`` with every NUL character replaced by U+FFFD.

    Postgres text and jsonb cannot hold U+0000, and a log line or a span attribute can carry
    one; left in, the evidence row is refused and the agent's whole job fails.
    """
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {without_nul(k): without_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [without_nul(v) for v in value]
    return value


def canonical_json(value: Json) -> str:
    """One fixed serialization, so equal data always gives equal bytes (and hashes)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
