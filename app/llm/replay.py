"""Record and replay LLM steps, keyed by ``(agent, prompt_version, request hash)``.

The request hash covers everything the model saw: system prompt, tools, transcript. Tool
results reach the transcript with content-derived evidence ids (ADR-0005), so replaying the
tools from their cassette and the model from this one reproduces a whole investigation
byte for byte, with no cluster and no model. Change a prompt, a tool summary or the loop's
wording and the hash changes: the replay misses loudly instead of answering a question it
was never asked.

A cassette is JSON lines like the tool cassettes: a header, then one entry per step.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.llm.types import LLMBackend, LLMError, LLMRequest, LLMResult, Usage
from app.tools.base import canonical_json


def replay_key(agent: str, prompt_version: str, request_hash: str) -> str:
    return f"{agent}|{prompt_version}|{request_hash}"


@dataclass
class LLMCassette:
    entries: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> LLMCassette:
        data = path.read_bytes()
        text = (gzip.decompress(data) if path.suffix == ".gz" else data).decode()
        lines = [line for line in text.splitlines() if line.strip()]
        header = json.loads(lines[0])
        if header.get("llm_cassette") != 1:
            raise ValueError(f"{path}: not a version 1 LLM cassette")
        return cls(entries=[json.loads(line) for line in lines[1:]], meta=header.get("meta", {}))

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [canonical_json({"llm_cassette": 1, "meta": self.meta}), *(canonical_json(e) for e in self.entries)]
        data = ("\n".join(lines) + "\n").encode()
        path.write_bytes(gzip.compress(data, mtime=0) if path.suffix == ".gz" else data)

    def add(self, request: LLMRequest, result: LLMResult) -> None:
        self.entries.append(
            {
                "agent": request.agent,
                "prompt_version": request.prompt_version,
                "request_hash": result.request_hash,
                "model": result.model,
                "step": result.step,
                "usage": result.usage.to_dict(),
                "cost_usd": result.cost_usd,
                "latency_ms": result.latency_ms,
                "backend": result.backend,
            }
        )


class ReplayBackend:
    name = "replay"

    def __init__(self, cassette: LLMCassette) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        for e in cassette.entries:
            # The same request twice (a retried job) was answered the same way; keep the first.
            self.entries.setdefault(replay_key(e["agent"], e["prompt_version"], e["request_hash"]), e)

    async def step(self, request: LLMRequest) -> LLMResult:
        request_hash = request.hash()
        entry = self.entries.get(replay_key(request.agent, request.prompt_version, request_hash))
        if entry is None:
            raise LLMError(
                "replay_miss",
                f"no recorded step for {request.agent} (prompt {request.prompt_version}, request {request_hash[:12]}); "
                "the prompt, the tools or the transcript changed since the recording",
            )
        return LLMResult(
            step=entry["step"],
            usage=Usage(**entry["usage"]),
            cost_usd=entry.get("cost_usd"),
            latency_ms=0,
            model=entry["model"],
            backend="replay",
            request_hash=request_hash,
            meta={"recorded_latency_ms": entry.get("latency_ms"), "recorded_backend": entry.get("backend")},
        )


class RecordingBackend:
    """A live backend whose every successful step is also appended to a cassette."""

    def __init__(self, live: LLMBackend, cassette: LLMCassette) -> None:
        self.live = live
        self.cassette = cassette
        self.name = live.name

    async def step(self, request: LLMRequest) -> LLMResult:
        result = await self.live.step(request)
        self.cassette.add(request, result)
        return result
