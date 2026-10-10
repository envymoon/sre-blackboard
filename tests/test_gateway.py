"""Gateway tests: routing, per-call cap, incident budgets, model pinning."""

import pytest

from app.gateway import PER_CALL_TOKEN_LIMIT, ModelGateway, TokenLedger, route
from app.settings import get_settings


def test_routing_uses_configured_model(monkeypatch):
    monkeypatch.setenv("SRE_LLM_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("SRE_LLM_ROLE_MODELS", '{"synthesizer": "claude-sonnet-5-5"}')
    get_settings.cache_clear()
    try:
        assert route("logs") == "claude-haiku-5-5"
        assert route("synthesizer") == "claude-sonnet-5-5"
        with pytest.raises(ValueError):
            route("unknown-role")
    finally:
        get_settings.cache_clear()


def test_per_call_limit():
    gw = ModelGateway(TokenLedger(incident_id="i1", priority="P0"))
    assert gw.complete("coordinator", PER_CALL_TOKEN_LIMIT + 1, 0)["status"] == "BUDGET_EXCEEDED"


def test_incident_budget():
    gw = ModelGateway(TokenLedger(incident_id="i2", priority="P3"))
    assert gw.complete("logs", 20_000, 2_000)["status"] == "ok"
    assert gw.complete("logs", 20_000, 2_000)["status"] == "BUDGET_EXCEEDED"


def test_model_pinned_per_incident(monkeypatch):
    ledger = TokenLedger(incident_id="i3")
    gw = ModelGateway(ledger)
    assert gw.complete("logs", 100, 10)["model_id"] == get_settings().llm_model
    monkeypatch.setitem(ledger.frozen, "logs", "pinned-model")
    assert gw.complete("logs", 100, 10)["model_id"] == "pinned-model"
