"""Where raw tool results come from: the live backends, or a cassette recorded from them.

``LiveSource`` adds the resilience: a timeout per attempt, a bounded number of retries for
errors that may go away, and a circuit breaker per backend so a dead Loki costs one fast
error instead of a timeout on every call.

A cassette is a JSON-lines file of ``(tool, args) -> raw result or error``, keyed on the
validated arguments. Replaying it returns the same raw data, so the summary and its hash
come out byte for byte the same; this is what lets CI and the cloud dev container run the
agent without a cluster.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.tools.base import Json, Tool, ToolArgs, ToolError, canonical_json
from app.tools.clients import Clients


@dataclass
class Fetched:
    raw: Json
    fetched_at: datetime
    attempts: int
    source: str  # "live" or "replay"


class Source(Protocol):
    async def fetch(self, tool: Tool, args: ToolArgs) -> Fetched: ...


@dataclass
class Breaker:
    """Opens after ``threshold`` retryable failures in a row; lets one call through after ``cooldown_s``."""

    threshold: int = 5
    cooldown_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    opened_at: float | None = None

    def check(self, backend: str) -> None:
        if self.opened_at is None:
            return
        if self.clock() - self.opened_at < self.cooldown_s:
            raise ToolError(
                "circuit_open",
                f"{backend} failed {self.failures} times in a row; skipped for now. Use another data source.",
                retryable=True,
            )
        self.opened_at = None  # half open: the next call is the trial

    def record(self, ok: bool) -> None:
        if ok:
            self.failures = 0
            self.opened_at = None
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()


@dataclass
class LiveSource:
    clients: Clients
    retries: int = 2
    backoff_s: float = 0.25
    breakers: dict[str, Breaker] = field(default_factory=dict)
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], Any] = asyncio.sleep

    def breaker(self, backend: str) -> Breaker:
        return self.breakers.setdefault(backend, Breaker())

    async def fetch(self, tool: Tool, args: ToolArgs) -> Fetched:
        breaker = self.breaker(tool.backend)
        attempt = 0
        while True:
            attempt += 1
            breaker.check(tool.backend)
            at = self.now()
            try:
                raw = await asyncio.wait_for(tool.fetch(self.clients, args, at), timeout=tool.timeout_s)
            except TimeoutError:
                err = ToolError("timeout", f"{tool.name} took longer than {tool.timeout_s:.0f}s", retryable=True)
            except ToolError as e:
                err = e
            else:
                breaker.record(ok=True)
                return Fetched(raw=raw, fetched_at=at, attempts=attempt, source="live")
            # Only backend trouble counts against the breaker; a bad query is the caller's problem.
            if err.retryable:
                breaker.record(ok=False)
            if not err.retryable or attempt > self.retries:
                err.attempts = attempt
                raise err
            await self.sleep(self.backoff_s * 2 ** (attempt - 1) * (1 + random.random() / 2))


def cassette_key(tool: str, args: ToolArgs) -> str:
    return canonical_json([tool, args.model_dump(mode="json")])


@dataclass
class Cassette:
    """Recorded calls, in order. The same key may appear several times (the data changes over time)."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Cassette:
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        header = json.loads(lines[0])
        if header.get("cassette") != 1:
            raise ValueError(f"{path}: not a version 1 cassette")
        return cls(entries=[json.loads(line) for line in lines[1:]], meta=header.get("meta", {}))

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [canonical_json({"cassette": 1, "meta": self.meta})]
        lines += [canonical_json(e) for e in self.entries]
        path.write_text("\n".join(lines) + "\n")


@dataclass
class RecordingSource:
    """Live calls, each one also appended to the cassette (errors included)."""

    live: Source
    cassette: Cassette

    async def fetch(self, tool: Tool, args: ToolArgs) -> Fetched:
        entry: dict[str, Any] = {"tool": tool.name, "args": args.model_dump(mode="json")}
        try:
            got = await self.live.fetch(tool, args)
        except ToolError as e:
            entry |= {"at": datetime.now(UTC).isoformat(), "error": {"code": e.code, "message": e.message}}
            self.cassette.entries.append(entry)
            raise
        self.cassette.entries.append(entry | {"at": got.fetched_at.isoformat(), "raw": got.raw})
        return got


@dataclass
class ReplaySource:
    """Answers from a cassette. Repeated calls get the recorded answers in order, then the last one again."""

    cassette: Cassette
    _queues: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _last: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for e in self.cassette.entries:
            key = canonical_json([e["tool"], e["args"]])
            self._queues.setdefault(key, []).append(e)

    async def fetch(self, tool: Tool, args: ToolArgs) -> Fetched:
        key = cassette_key(tool.name, args)
        queue = self._queues.get(key)
        if queue:
            entry = queue.pop(0)
            self._last[key] = entry
        elif key in self._last:
            entry = self._last[key]
        else:
            raise ToolError("replay_miss", f"no recorded answer for {tool.name} {args.model_dump(mode='json')}")
        if "error" in entry:
            raise ToolError(entry["error"]["code"], entry["error"]["message"])
        return Fetched(raw=entry["raw"], fetched_at=datetime.fromisoformat(entry["at"]), attempts=1, source="replay")
