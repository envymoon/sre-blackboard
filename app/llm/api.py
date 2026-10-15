"""AnthropicAPIBackend: the Messages API with native tool use and prompt caching.

Tool calls are real ``tool_use`` blocks and results go back as ``tool_result`` blocks. The final
answer is one more tool, ``submit_final``, whose input schema is the final answer's schema;
``tool_choice: any`` makes every step either tool calls or the final answer, never prose.

Caching: the tool definitions, the system prompt and the transcript so far are each marked
as cache breakpoints, so a step pays full price only for what the previous step added.

Plain httpx rather than the SDK: one endpoint, and the same client stack as the tools.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.llm.types import LLMError, LLMRequest, LLMResult, Turn, Usage, normalize_step

FINAL_TOOL = "submit_final"
API_VERSION = "2023-06-01"
_CACHE = {"type": "ephemeral"}


class AnthropicAPIBackend:
    name = "api"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.anthropic.com",
        timeout_s: float = 120.0,
        max_tokens: int = 4096,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"},
        )
        self.max_tokens = max_tokens

    async def aclose(self) -> None:
        await self.client.aclose()

    def body(self, request: LLMRequest) -> dict[str, Any]:
        final_tool = {
            "name": FINAL_TOOL,
            "description": "Give your final answer. Call it alone, when you have enough evidence.",
            "input_schema": request.final_schema,
        }
        tools = [*request.tools, final_tool]
        tools[-1] = {**tools[-1], "cache_control": _CACHE}
        tool_choice: dict[str, Any] = (
            {"type": "any", "disable_parallel_tool_use": False}
            if request.tools
            else {"type": "tool", "name": FINAL_TOOL}
        )
        messages = render_messages(request.turns)
        last = messages[-1]["content"][-1]
        messages[-1]["content"][-1] = {**last, "cache_control": _CACHE}
        return {
            "model": request.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": request.system, "cache_control": _CACHE}],
            "tools": tools,
            "tool_choice": tool_choice,
            "messages": messages,
        }

    async def step(self, request: LLMRequest) -> LLMResult:
        started = time.monotonic()
        try:
            resp = await self.client.post("/v1/messages", json=self.body(request))
        except httpx.TimeoutException as e:
            raise LLMError("timeout", f"Messages API: {e!r}", retryable=True) from e
        except httpx.HTTPError as e:
            raise LLMError("unavailable", f"Messages API: {e!r}", retryable=True) from e
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            raise classify_status(resp)
        data = resp.json()
        if data.get("stop_reason") == "max_tokens":
            raise LLMError("invalid_output", f"the step was cut off at {self.max_tokens} tokens", retryable=True)
        usage = data.get("usage") or {}
        return LLMResult(
            step=normalize_step(parse_content(data.get("content") or []), request),
            usage=Usage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            ),
            cost_usd=None,  # the API reports tokens, not prices
            latency_ms=latency_ms,
            model=str(data.get("model") or request.model),
            backend="api",
            request_hash=request.hash(),
        )


def render_messages(turns: tuple[Turn, ...] | list[Turn]) -> list[dict[str, Any]]:
    """Our turns as alternating user / assistant messages with tool_use and tool_result blocks."""
    messages: list[dict[str, Any]] = []
    pending_final: str | None = None  # id of a submit_final call that needs a tool_result next
    for i, turn in enumerate(turns):
        kind = turn["type"]
        if kind == "user":
            if pending_final is not None:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": pending_final,
                    "content": turn["text"],
                    "is_error": True,
                }
                pending_final = None
            else:
                block = {"type": "text", "text": turn["text"]}
            _append(messages, "user", block)
        elif kind == "step":
            blocks: list[dict[str, Any]] = []
            if turn.get("reasoning"):
                blocks.append({"type": "text", "text": turn["reasoning"]})
            for a in turn["actions"]:
                blocks.append({"type": "tool_use", "id": a["id"], "name": a["tool"], "input": a["args"]})
            if turn.get("final") is not None:
                pending_final = f"final_{i}"
                blocks.append({"type": "tool_use", "id": pending_final, "name": FINAL_TOOL, "input": turn["final"]})
            for b in blocks:
                _append(messages, "assistant", b)
        elif kind == "results":
            for r in turn["results"]:
                _append(messages, "user", {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]})
        else:
            raise ValueError(f"unknown turn type {kind!r}")
    return messages


def _append(messages: list[dict[str, Any]], role: str, block: dict[str, Any]) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].append(block)
    else:
        messages.append({"role": role, "content": [block]})


def parse_content(content: list[dict[str, Any]]) -> dict[str, Any]:
    reasoning = " ".join(b.get("text", "").strip() for b in content if b.get("type") == "text").strip()
    calls = [b for b in content if b.get("type") == "tool_use"]
    finals = [b for b in calls if b.get("name") == FINAL_TOOL]
    actions = [{"tool": b["name"], "args": b.get("input") or {}} for b in calls if b.get("name") != FINAL_TOOL]
    final = finals[0].get("input") if finals and not actions else None
    return {"reasoning": reasoning, "actions": actions, "final": final}


def classify_status(resp: httpx.Response) -> LLMError:
    try:
        detail = resp.json().get("error", {}).get("message", "")
    except ValueError:
        detail = resp.text[:300]
    message = f"HTTP {resp.status_code}: {detail}"
    if resp.status_code == 429:
        try:
            retry_after: float | None = float(resp.headers.get("retry-after", ""))
        except ValueError:
            retry_after = None
        return LLMError("rate_limited", message, retryable=True, retry_after_s=retry_after)
    if resp.status_code == 529:
        return LLMError("overloaded", message, retryable=True)
    if resp.status_code >= 500:
        return LLMError("unavailable", message, retryable=True)
    if resp.status_code in (401, 403):
        return LLMError("auth", message)
    return LLMError("bad_request", message)
