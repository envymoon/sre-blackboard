"""ClaudeCLIBackend: one step = one ``claude -p`` process, on the subscription's quota.

The flags make the CLI a plain inference engine (see docs/learn/m4-agent.md for the probe):
our system prompt replaces Claude Code's, every built-in tool, MCP server and settings file is
off, and ``--json-schema`` makes it return the step as ``structured_output``. The model cannot
do anything itself; it can only propose tool calls, which the harness runs.

Failure modes, as the probe saw them: a non-zero exit with ``is_error: true`` and an
``api_error_status`` (404 for an unknown model), or a non-zero exit with no JSON at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from collections.abc import Sequence
from typing import Any

from app.llm.render import step_schema, system_text, transcript_text
from app.llm.types import LLMError, LLMRequest, LLMResult, Usage, normalize_step

# Environment variables that would make the child think it runs inside another Claude Code
# session (the cloud dev container sets them) or point it at our own settings.
_DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
_RATE_LIMIT = re.compile(r"rate.?limit|usage limit|limit reached|too many requests|quota", re.I)
# Said last, right before the model answers. Without it Haiku first tries to call a data source
# as a native tool, fails, and only then answers (a wasted turn; see the M4 probe).
CLI_INSTRUCTION = (
    "Reply with your next step by calling StructuredOutput, the only tool you have. "
    "Data sources are requested inside it, in `actions`."
)
_RESET_AT = re.compile(r"\|(\d{10})\b")  # "Claude AI usage limit reached|1760000000"


class ClaudeCLIBackend:
    name = "cli"

    def __init__(
        self,
        *,
        binary: str = "claude",
        timeout_s: float = 180.0,
        concurrency: int = 2,
        extra_args: Sequence[str] = (),
    ) -> None:
        self.binary = binary
        self.timeout_s = timeout_s
        # Every process holds ~250 MB and the subscription has a rate window: few at a time.
        self.semaphore = asyncio.Semaphore(concurrency)
        self.extra_args = tuple(extra_args)
        # An empty working directory, so no CLAUDE.md or project settings are picked up.
        self.workdir = tempfile.mkdtemp(prefix="sre-llm-")

    def command(self, request: LLMRequest) -> list[str]:
        return [
            self.binary,
            "-p",
            "--model",
            request.model,
            "--system-prompt",
            system_text(request),
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--json-schema",
            json.dumps(step_schema(request)),
            "--output-format",
            "json",
            "--no-session-persistence",
            *self.extra_args,
        ]

    async def step(self, request: LLMRequest) -> LLMResult:
        prompt = transcript_text(request.turns, CLI_INSTRUCTION).encode()
        env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
        async with self.semaphore:
            started = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command(request),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.workdir,
                    env=env,
                )
            except FileNotFoundError as e:
                raise LLMError("unavailable", f"{self.binary} not found; install Claude Code") from e
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(prompt), self.timeout_s)
            except TimeoutError as e:
                proc.kill()
                await proc.wait()
                raise LLMError("timeout", f"claude -p took longer than {self.timeout_s:.0f}s", retryable=True) from e
            latency_ms = int((time.monotonic() - started) * 1000)
        return parse_output(request, proc.returncode or 0, stdout, stderr, latency_ms)


def parse_output(request: LLMRequest, returncode: int, stdout: bytes, stderr: bytes, latency_ms: int) -> LLMResult:
    try:
        out: dict[str, Any] = json.loads(stdout)
    except ValueError:
        out = {}
    if returncode != 0 or out.get("is_error") or not out:
        raise classify_error(returncode, out, stderr.decode(errors="replace"))
    structured = out.get("structured_output")
    if structured is None:
        raise LLMError(
            "invalid_output",
            f"no structured_output (subtype={out.get('subtype')}, result={str(out.get('result'))[:200]!r})",
            retryable=True,
        )
    usage = out.get("usage") or {}
    models = list((out.get("modelUsage") or {}).keys())
    return LLMResult(
        step=normalize_step(structured, request),
        usage=Usage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        ),
        cost_usd=out.get("total_cost_usd"),
        latency_ms=latency_ms,
        model=models[0] if len(models) == 1 else request.model,
        backend="cli",
        request_hash=request.hash(),
        meta={"num_turns": out.get("num_turns"), "duration_api_ms": out.get("duration_api_ms")},
    )


def classify_error(returncode: int, out: dict[str, Any], stderr: str) -> LLMError:
    status = out.get("api_error_status")
    text = f"{out.get('result') or ''} {stderr}".strip()
    message = f"exit {returncode}" + (f", HTTP {status}" if status else "") + (f": {text[:300]}" if text else "")
    if status == 429 or _RATE_LIMIT.search(text):
        reset = _RESET_AT.search(text)
        retry_after = max(0.0, int(reset.group(1)) - time.time()) if reset else None
        return LLMError("rate_limited", message, retryable=True, retry_after_s=retry_after)
    if status in (401, 403) or re.search(r"not logged in|invalid api key|authenticat|/login", text, re.I):
        return LLMError("auth", message)
    if status in (400, 404, 413):
        return LLMError("bad_request", message)
    if status is None or status in (500, 502, 503, 504, 529):
        # No status at all is a crash or a network failure inside the CLI: worth one more try.
        return LLMError("overloaded" if status == 529 else "unavailable", message, retryable=True)
    return LLMError("bad_request", message)
