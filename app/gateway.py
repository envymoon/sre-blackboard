"""Model gateway: per-role model routing plus per-call and per-incident token budgets.

This is the budget-accounting half of the LLM layer. It does not call a model yet;
the pluggable backends (Claude CLI, Anthropic API, replay) arrive in milestone M4.
Costs are not estimated here: they will be read from the usage each backend returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .settings import get_settings

AGENT_ROLES = {"coordinator", "metrics", "logs", "traces", "changes", "synthesizer"}

PER_CALL_TOKEN_LIMIT = 32_000

INCIDENT_BUDGETS = {
    "P0": {"input": 120_000, "output": 18_000},
    "P1": {"input": 80_000, "output": 12_000},
    "P2": {"input": 40_000, "output": 6_000},
    "P3": {"input": 24_000, "output": 3_000},
}


def route(agent: str) -> str:
    """Return the model ID configured for an agent role."""
    role = agent.lower()
    if role not in AGENT_ROLES:
        raise ValueError(f"unknown agent role: {agent}")
    settings = get_settings()
    return settings.llm_role_models.get(role, settings.llm_model)


@dataclass
class TokenLedger:
    incident_id: str
    priority: str = "P0"
    input_used: int = 0
    output_used: int = 0
    # role -> model ID, pinned on first use so an incident never switches models mid-run
    frozen: dict[str, str] = field(default_factory=dict)


class ModelGateway:
    """Enforces the per-call cap and the per-incident budget before any model call."""

    def __init__(self, ledger: TokenLedger):
        self.ledger = ledger

    def complete(self, agent: str, input_tokens: int, output_tokens: int) -> dict:
        model_id = self.ledger.frozen.setdefault(agent.lower(), route(agent))
        if input_tokens + output_tokens > PER_CALL_TOKEN_LIMIT:
            return {"status": "BUDGET_EXCEEDED", "reason": f"per-call limit {PER_CALL_TOKEN_LIMIT}"}
        budget = INCIDENT_BUDGETS[self.ledger.priority]
        if (
            self.ledger.input_used + input_tokens > budget["input"]
            or self.ledger.output_used + output_tokens > budget["output"]
        ):
            return {"status": "BUDGET_EXCEEDED", "reason": f"incident {self.ledger.priority} budget"}
        self.ledger.input_used += input_tokens
        self.ledger.output_used += output_tokens
        return {
            "status": "ok",
            "model_id": model_id,
            "usage": {"input": self.ledger.input_used, "output": self.ledger.output_used},
        }
