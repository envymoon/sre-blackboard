"""Model Gateway: dual-model routing + budgets + billing.
Maps to spec Sec.3 plus the model_freeze_keys config and harness_adapter_required flag.

primary=muse-spark-1.3 (xhigh) with a 64K per-call hard cap;
fast=gemini-3.8-flash (medium, triage uses low) with a 32K cap.
Thinking tokens always bill as output. Fast intro pricing runs through 2026-12-31.
The Harness only calls complete/estimate/capabilities; malformed output gets
exactly one format repair with no additional tool calls.
"""
from __future__ import annotations
from dataclasses import dataclass, field

PRIMARY_MODEL_ID = "muse-spark-1.3"
PRIMARY_TIER = "xhigh"
PRIMARY_HARD_LIMIT = 64000
PRIMARY_RATES = {"input": 1.25, "output": 4.25, "cache_read": 0.15}

FAST_MODEL_ID = "gemini-3.8-flash"
FAST_LEVEL_DEFAULT = "medium"
FAST_LEVEL_TRIAGE = "low"
FAST_HARD_LIMIT = 32000
FAST_RATES_INTRO = {"input": 0.75, "output": 3.75, "cache_read": 0.075}

PRIMARY_AGENTS = {"coordinator", "safety", "code_analysis", "remediation"}
FAST_AGENTS = {"triage", "metrics", "log", "trace", "host", "database", "change", "dependency", "reporter"}

# Sec.3.3 per-incident budgets
INCIDENT_BUDGETS = {
    "P0": {"input": 120000, "output": 18000},
    "P1": {"input": 80000, "output": 12000},
    "P2": {"input": 40000, "output": 6000},
    "P3": {"input": 24000, "output": 3000},
}


def route(agent: str) -> dict:
    a = agent.lower()
    if a in PRIMARY_AGENTS:
        return {"model_id": PRIMARY_MODEL_ID, "tier": PRIMARY_TIER, "hard_limit": PRIMARY_HARD_LIMIT}
    if a in FAST_AGENTS:
        level = FAST_LEVEL_TRIAGE if a == "triage" else FAST_LEVEL_DEFAULT
        return {"model_id": FAST_MODEL_ID, "thinking_level": level, "hard_limit": FAST_HARD_LIMIT}
    raise ValueError(f"unknown agent role: {agent}")


def call_cost(model_id: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    rates = PRIMARY_RATES if model_id == PRIMARY_MODEL_ID else FAST_RATES_INTRO
    uncached = max(0, input_tokens - cached_tokens)
    return cached_tokens / 1e6 * rates["cache_read"] + uncached / 1e6 * rates["input"] + output_tokens / 1e6 * rates["output"]


def estimate_p0_cost() -> float:
    # Sec.3.3 convention: 70% of input hits cache; 40% of input on primary / 60% on fast; 60% of output on primary / 40% on fast
    total_in, total_out = 120000, 18000
    cached = int(total_in * 0.7)
    pin, fin = int(total_in * 0.4), total_in - int(total_in * 0.4)
    pout, fout = int(total_out * 0.6), total_out - int(total_out * 0.6)
    pcached, fcached = int(cached * 0.4), cached - int(cached * 0.4)
    return round(call_cost(PRIMARY_MODEL_ID, pin, pout, pcached) + call_cost(FAST_MODEL_ID, fin, fout, fcached), 4)


@dataclass
class TokenLedger:
    incident_id: str
    priority: str = "P0"
    input_used: int = 0
    output_used: int = 0
    frozen: dict = field(default_factory=dict)

    def freeze(self, primary_id: str, tier: str, fast_id: str, level: str):
        self.frozen = {"primary_model_id": primary_id, "primary_reasoning_tier": tier,
                       "fast_model_id": fast_id, "fast_thinking_level_default": level}

    def check_frozen(self, primary_id: str, tier: str, fast_id: str, level: str) -> bool:
        if not self.frozen:
            return True
        return self.frozen == {"primary_model_id": primary_id, "primary_reasoning_tier": tier,
                               "fast_model_id": fast_id, "fast_thinking_level_default": level}


class ModelGateway:
    """The Harness's only model entrypoint. complete() enforces the per-call hard cap
    plus the per-incident budget, returning BUDGET_EXCEEDED on overflow."""

    def __init__(self, ledger: TokenLedger):
        self.ledger = ledger
        self.ledger.freeze(PRIMARY_MODEL_ID, PRIMARY_TIER, FAST_MODEL_ID, FAST_LEVEL_DEFAULT)

    def capabilities(self) -> dict:
        return {"primary": PRIMARY_MODEL_ID, "fast": FAST_MODEL_ID, "adapter_required": True}

    def estimate(self, agent: str, input_tokens: int, output_tokens: int) -> dict:
        r = route(agent)
        mid = r["model_id"]
        return {"model_id": mid, "cost_usd": call_cost(mid, input_tokens, output_tokens, int(input_tokens * 0.7))}

    def complete(self, agent: str, input_tokens: int, output_tokens: int, format_error: bool = False) -> dict:
        """With format_error=True exactly one repair is allowed (the caller retries once); only flagged here, no new call."""
        r = route(agent)
        if input_tokens + output_tokens > r["hard_limit"]:
            return {"status": "BUDGET_EXCEEDED", "reason": f"per-call hard limit {r['hard_limit']}"}
        budget = INCIDENT_BUDGETS[self.ledger.priority]
        if self.ledger.input_used + input_tokens > budget["input"] or self.ledger.output_used + output_tokens > budget["output"]:
            return {"status": "BUDGET_EXCEEDED", "reason": f"incident {self.ledger.priority} budget"}
        self.ledger.input_used += input_tokens
        self.ledger.output_used += output_tokens
        cost = call_cost(r["model_id"], input_tokens, output_tokens, int(input_tokens * 0.7))
        out = {"status": "ok", "model_id": r["model_id"], "cost_usd": round(cost, 6),
               "usage": {"input": self.ledger.input_used, "output": self.ledger.output_used}}
        out.update(r)
        if format_error:
            out["format_repaired_once"] = True
        return out
