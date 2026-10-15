"""A model stand-in for agent tests: each agent follows a script, and the steps can read the
transcript (to cite the evidence ids the harness actually returned)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.llm.types import LLMError, LLMRequest, LLMResult, Turn, Usage

Script = Callable[[LLMRequest, int], dict[str, Any]]


def last_results(turns: tuple[Turn, ...]) -> list[dict[str, Any]]:
    """The tool results of the latest results turn, parsed."""
    for turn in reversed(turns):
        if turn["type"] == "results":
            return [json.loads(r["content"]) for r in turn["results"]]
    return []


def evidence_ids(turns: tuple[Turn, ...], tool: str | None = None) -> list[str]:
    out = []
    for turn in turns:
        if turn["type"] == "results":
            for r in turn["results"]:
                body = json.loads(r["content"])
                if body.get("evidence_id") and (tool is None or body["tool"] == tool):
                    out.append(body["evidence_id"])
    return out


def steps_taken(turns: tuple[Turn, ...]) -> int:
    return sum(1 for t in turns if t["type"] == "step")


class ScriptedLLM:
    name = "replay"

    def __init__(self, scripts: dict[str, Script], *, tokens_per_step: int = 1000) -> None:
        self.scripts = scripts
        self.requests: list[LLMRequest] = []
        self.tokens_per_step = tokens_per_step
        self.fail: dict[str, LLMError] = {}

    def calls(self, agent: str) -> int:
        return sum(1 for r in self.requests if r.agent == agent)

    async def step(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if request.agent in self.fail:
            raise self.fail[request.agent]
        raw = self.scripts[request.agent](request, steps_taken(request.turns) + 1)
        step = {
            "reasoning": raw.get("reasoning", "scripted"),
            "actions": raw.get("actions", []),
            "final": raw.get("final"),
        }
        return LLMResult(
            step=step,
            usage=Usage(input_tokens=10, output_tokens=self.tokens_per_step - 10),
            cost_usd=0.001,
            latency_ms=1,
            model=request.model,
            backend="replay",
            request_hash=request.hash(),
        )
