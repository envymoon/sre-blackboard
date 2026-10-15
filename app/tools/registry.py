"""The one way tools are called: validate, fetch, check size, summarize, store, log.

Every call ends in a ToolOutcome, never an exception: a model gets either a summary with an
evidence id, or a structured error it can act on (fix the query, narrow the window, use
another source). Only successful calls create evidence; every call is logged.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolOutcome, canonical_json, sha256_hex
from app.tools.compress import MAX_SUMMARY_CHARS, bound
from app.tools.evidence import EvidenceRecord, EvidenceSink, ToolCallRecord, evidence_id
from app.tools.sources import Source


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool],
        source: Source,
        sink: EvidenceSink,
        max_summary_chars: int = MAX_SUMMARY_CHARS,
    ) -> None:
        self.tools = {t.name: t for t in tools}
        self.source = source
        self.sink = sink
        self.max_summary_chars = max_summary_chars

    def specs(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Tool definitions for the model, optionally only the named ones (per agent role)."""
        chosen = list(names) if names is not None else sorted(self.tools)
        return [self.tools[n].spec() for n in chosen]

    async def call(self, name: str, raw_args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        started = time.monotonic()
        outcome = ToolOutcome(tool=name, args=dict(raw_args))
        evidence: EvidenceRecord | None = None
        try:
            tool = self._tool(name)
            args = _validate(tool, raw_args)
            outcome.args = args.model_dump(mode="json")
            evidence = await self._run(tool, args, ctx, outcome)
        except ToolError as e:
            outcome.error = e
            outcome.attempts = e.attempts
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        await self.sink.save(
            evidence,
            ToolCallRecord(
                tenant_id=ctx.tenant_id,
                incident_id=ctx.incident_id,
                agent=ctx.agent,
                tool=name,
                args=outcome.args,
                status="ok" if outcome.ok else "error",
                error_code=outcome.error.code if outcome.error else None,
                error=outcome.error.message if outcome.error else None,
                attempts=outcome.attempts,
                latency_ms=outcome.latency_ms,
                evidence_id=outcome.evidence_id,
            ),
        )
        return outcome

    def _tool(self, name: str) -> Tool:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError("unknown_tool", f"no tool named {name!r}; available: {', '.join(sorted(self.tools))}")
        return tool

    async def _run(self, tool: Tool, args: ToolArgs, ctx: ToolContext, outcome: ToolOutcome) -> EvidenceRecord:
        fetched = await self.source.fetch(tool, args, ctx.agent)
        outcome.attempts = fetched.attempts
        outcome.fetched_at = fetched.fetched_at
        raw_json = canonical_json(fetched.raw)
        size = len(raw_json.encode())
        if size > tool.max_result_bytes:
            raise ToolError(
                "too_large",
                f"the result is {size // 1024} KiB, over the {tool.max_result_bytes // 1024} KiB limit; "
                "narrow the query (fewer series, a shorter window, a more specific filter)",
            )
        try:
            summary = tool.summarize(fetched.raw, args)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            raise ToolError("backend_error", f"unexpected response shape from {tool.backend}: {e!r}") from e
        summary, truncated = bound(summary, self.max_summary_chars)
        result_hash = sha256_hex(raw_json)
        record = EvidenceRecord(
            id=evidence_id(ctx.evidence_scope, tool.name, outcome.args, result_hash),
            tenant_id=ctx.tenant_id,
            incident_id=ctx.incident_id,
            tool=tool.name,
            args=outcome.args,
            result=fetched.raw,
            result_hash=result_hash,
            result_bytes=size,
            summary=summary,
            truncated=truncated,
            source=fetched.source,
            fetched_at=fetched.fetched_at,
        )
        outcome.evidence_id = record.id
        outcome.summary = summary
        outcome.meta = {"result_hash": record.result_hash, "result_bytes": size, "truncated": truncated}
        return record


def _validate(tool: Tool, raw_args: dict[str, Any]) -> ToolArgs:
    try:
        return tool.args.model_validate(raw_args)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(args)'}: {err['msg']}" for err in e.errors()[:5]
        )
        raise ToolError("invalid_args", f"{tool.name}: {problems}") from e
