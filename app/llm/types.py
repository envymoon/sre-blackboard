"""What every LLM backend takes and returns.

The agent loop is ours (app/agents/loop.py); a backend only answers "what is the next step?"
for one rendered transcript. A step is either up to a few tool calls or the final answer:

    {"reasoning": "...", "actions": [{"tool": "prom_query", "args": {...}}], "final": null}
    {"reasoning": "...", "actions": [], "final": {...}}

The transcript is a list of plain JSON turns, so it can be hashed for replay and stored:

    {"type": "user", "text": "..."}                                  task, or feedback on a final
    {"type": "step", "reasoning": "...", "actions": [{"id", "tool", "args"}], "final": ...}
    {"type": "results", "results": [{"id", "tool", "content"}]}       one per action, same ids

Each backend renders the turns its own way: the CLI as one text prompt with a JSON schema
for the step, the API as native tool_use / tool_result blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.tools.base import canonical_json, sha256_hex

Turn = dict[str, Any]
BackendName = Literal["cli", "api", "replay"]


@dataclass(frozen=True)
class LLMRequest:
    agent: str  # role instance, e.g. "investigator.metrics"; also the replay key's first part
    model: str
    prompt_version: str
    system: str
    turns: tuple[Turn, ...]
    # Anthropic tool definitions (name, description, input_schema). Empty: the step can only be final.
    tools: tuple[dict[str, Any], ...]
    # JSON schema of the final answer.
    final_schema: dict[str, Any]
    max_actions: int = 3
    # The last step an agent may take: no more data, the final answer now.
    force_final: bool = False

    def hash(self) -> str:
        """Identifies the request for replay: everything the model sees, nothing else."""
        return sha256_hex(
            canonical_json(
                {
                    "model": self.model,
                    "system": self.system,
                    "turns": list(self.turns),
                    "tools": list(self.tools),
                    "final_schema": self.final_schema,
                    "max_actions": self.max_actions,
                    "force_final": self.force_final,
                }
            )
        )


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Everything the model read. With caching most of it is in the two cache counters."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }


@dataclass(frozen=True)
class LLMResult:
    step: dict[str, Any]  # {"reasoning", "actions", "final"}, shape-checked by the backend
    usage: Usage
    # Equivalent API cost as reported by the backend (the CLI reports it; the API does not).
    cost_usd: float | None
    latency_ms: int
    model: str  # the model that answered
    backend: BackendName
    request_hash: str
    attempts: int = 1
    meta: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    """A failed step. ``code`` is one of: rate_limited, overloaded, timeout, auth, bad_request,
    invalid_output, replay_miss, unavailable."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, retry_after_s: float | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.attempts = 1


class LLMBackend(Protocol):
    name: str

    async def step(self, request: LLMRequest) -> LLMResult: ...


def normalize_step(raw: Any, request: LLMRequest) -> dict[str, Any]:
    """Coerce a backend's answer into the step shape, or raise invalid_output.

    Only the shape is checked here; whether the tool arguments and the final answer are valid
    is the loop's business (it tells the model what was wrong).
    """
    if not isinstance(raw, dict):
        raise LLMError("invalid_output", f"step is {type(raw).__name__}, not an object", retryable=True)
    reasoning = raw.get("reasoning")
    actions = raw.get("actions") or []
    final = raw.get("final")
    if not isinstance(reasoning, str):
        reasoning = ""
    if not isinstance(actions, list) or not all(
        isinstance(a, dict) and isinstance(a.get("tool"), str) and isinstance(a.get("args", {}), dict) for a in actions
    ):
        raise LLMError("invalid_output", "actions must be a list of {tool, args}", retryable=True)
    if final is None and not actions:
        raise LLMError("invalid_output", "a step needs actions or a final answer", retryable=True)
    if final is not None and actions:
        final = None  # the model wants more data first; the final answer can come next step
    return {
        "reasoning": reasoning,
        "actions": [{"tool": a["tool"], "args": a.get("args", {})} for a in actions],
        "final": final,
    }
