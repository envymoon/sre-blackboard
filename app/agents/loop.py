"""The agent loop, written by hand and the same for every backend.

    render the transcript -> backend.step() -> check the step
      actions: run them through the tool registry (in parallel), append the results, repeat
      final:   validate it against the schema, check its citations;
               problems go back to the model once, then whatever still fails is dropped

The model only proposes; the harness decides what runs. A tool outside the agent's role is
answered with an error, not run. The last allowed step must be the final answer (the
backend is told so), and the incident's token budget is checked before every step.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from app.agents.citations import Checked
from app.agents.prompts import Prompt
from app.agents.store import AgentStore, LLMCallRecord
from app.llm.render import inline_refs
from app.llm.types import LLMBackend, LLMError, LLMRequest, Turn, Usage
from app.tools.base import ToolContext, ToolError, ToolOutcome
from app.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

MAX_ACTIONS = 3
LAST_STEP_NOTE = "You have no more query steps. Give your final answer now, from the results you have."


class BudgetExceeded(Exception):
    """The incident used up its token budget; it goes to a human (ESCALATED)."""


class AgentFailed(Exception):
    """The agent could not produce a usable answer (the model kept answering out of shape)."""


@dataclass(frozen=True)
class AgentSpec:
    name: str  # e.g. "investigator.metrics"
    prompt: Prompt
    tools: tuple[str, ...]
    final_model: type[BaseModel]
    max_steps: int = 8
    max_rewrites: int = 1


@dataclass
class AgentRun:
    final: BaseModel | None = None
    steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    rewrites: int = 0
    dropped: int = 0
    problems: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    shown: dict[str, str] = field(default_factory=dict)  # evidence id -> tool, as shown to the agent
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "rewrites": self.rewrites,
            "dropped": self.dropped,
            "problems": self.problems[:10],
            "usage": self.usage.to_dict(),
            "cost_usd": round(self.cost_usd, 6),
        }


Check = Callable[[Any, dict[str, str]], Awaitable[Checked[Any]]]


@dataclass(frozen=True)
class Deps:
    backend: LLMBackend
    registry: ToolRegistry
    store: AgentStore
    tenant_id: str
    incident_id: UUID
    token_budget: int


def final_schema(model: type[BaseModel]) -> dict[str, Any]:
    return inline_refs(model.model_json_schema())


async def run_agent(spec: AgentSpec, task: str, *, model: str, deps: Deps, check: Check) -> AgentRun:
    run = AgentRun(turns=[{"type": "user", "text": task}])
    tool_specs = tuple(deps.registry.specs(spec.tools)) if spec.tools else ()
    schema = final_schema(spec.final_model)
    ctx = ToolContext(tenant_id=deps.tenant_id, incident_id=deps.incident_id, agent=spec.name)
    while True:
        run.steps += 1
        used = await deps.store.tokens_used(deps.incident_id)
        if used >= deps.token_budget:
            raise BudgetExceeded(f"{used} tokens used, budget {deps.token_budget}")
        request = LLMRequest(
            agent=spec.name,
            model=model,
            prompt_version=spec.prompt.version,
            system=spec.prompt.text,
            turns=tuple(run.turns),
            tools=tool_specs,
            final_schema=schema,
            max_actions=MAX_ACTIONS,
            force_final=bool(tool_specs) and run.steps >= spec.max_steps,
        )
        step = await _step(spec, request, run, deps)
        actions = [{"id": f"c{run.steps}_{i}", **a} for i, a in enumerate(step["actions"][:MAX_ACTIONS], 1)]
        run.turns.append({"type": "step", "reasoning": step["reasoning"], "actions": actions, "final": step["final"]})

        if step["final"] is None:
            if request.force_final:
                raise AgentFailed(f"{spec.name}: no final answer on the last step")
            results = await asyncio.gather(*(_call(spec, deps.registry, a, ctx) for a in actions))
            for outcome in results:
                run.tool_calls += 1
                if outcome.ok and outcome.evidence_id:
                    run.shown[outcome.evidence_id] = outcome.tool
                else:
                    run.tool_errors += 1
            run.turns.append(
                {
                    "type": "results",
                    "results": [
                        {"id": a["id"], "tool": a["tool"], "content": o.for_model()}
                        for a, o in zip(actions, results, strict=True)
                    ],
                }
            )
            if run.steps + 1 >= spec.max_steps:
                run.turns.append({"type": "user", "text": LAST_STEP_NOTE})
            continue

        try:
            parsed = spec.final_model.model_validate(step["final"])
        except ValidationError as e:
            problems = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:8]]
            if run.rewrites < spec.max_rewrites:
                _feedback(run, problems)
                continue
            raise AgentFailed(f"{spec.name}: final answer does not match the schema: {problems}") from e
        checked = await check(parsed, run.shown)
        run.problems = checked.problems
        if checked.problems and run.rewrites < spec.max_rewrites:
            _feedback(run, checked.problems)
            continue
        run.final = checked.value
        run.dropped = checked.dropped
        return run


def _feedback(run: AgentRun, problems: list[str]) -> None:
    run.rewrites += 1
    listed = "\n".join(f"- {p}" for p in problems)
    run.turns.append(
        {
            "type": "user",
            "text": f"Your final answer was not accepted:\n{listed}\n"
            "Fix these and give the final answer again. Items that still fail will be dropped.",
        }
    )


async def _step(spec: AgentSpec, request: LLMRequest, run: AgentRun, deps: Deps) -> dict[str, Any]:
    record = LLMCallRecord(
        tenant_id=deps.tenant_id,
        incident_id=deps.incident_id,
        agent=spec.name,
        step_no=run.steps,
        backend=deps.backend.name,
        model=request.model,
        prompt_version=request.prompt_version,
        request_hash=request.hash(),
        status="error",
    )
    try:
        result = await deps.backend.step(request)
    except LLMError as e:
        await deps.store.record_llm_call(
            replace(record, error_code=e.code, error=e.message[:2000], attempts=e.attempts)
        )
        raise
    await deps.store.record_llm_call(
        replace(
            record,
            status="ok",
            backend=result.backend,
            model=result.model,
            usage=result.usage,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            attempts=result.attempts,
            step=result.step,
        )
    )
    u = run.usage
    run.usage = Usage(
        u.input_tokens + result.usage.input_tokens,
        u.output_tokens + result.usage.output_tokens,
        u.cache_read_tokens + result.usage.cache_read_tokens,
        u.cache_creation_tokens + result.usage.cache_creation_tokens,
    )
    run.cost_usd += result.cost_usd or 0.0
    log.info(
        "%s step %d: %d action(s)%s, %d tokens, %d ms",
        spec.name,
        run.steps,
        len(result.step["actions"]),
        ", final" if result.step["final"] is not None else "",
        result.usage.total,
        result.latency_ms,
    )
    return result.step


async def _call(spec: AgentSpec, registry: ToolRegistry, action: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    if action["tool"] not in spec.tools:
        return ToolOutcome(
            tool=action["tool"],
            args=action["args"],
            error=ToolError(
                "unknown_tool", f"{action['tool']} is not one of your data sources: {', '.join(spec.tools)}"
            ),
        )
    return await registry.call(action["tool"], action["args"], ctx)
