"""Gateway tests: routing / hard caps / incident budgets / billing convention / freeze. Maps to spec Sec.3."""
from app.gateway import (route, call_cost, estimate_p0_cost, ModelGateway, TokenLedger,
                         PRIMARY_HARD_LIMIT, FAST_HARD_LIMIT)


def test_routing():
    assert route("coordinator")["model_id"] == "muse-spark-1.3"
    assert route("safety")["tier"] == "xhigh"
    assert route("log")["model_id"] == "gemini-3.8-flash"
    assert route("triage")["thinking_level"] == "low"
    assert route("reporter")["thinking_level"] == "medium"


def test_hard_limits():
    gw = ModelGateway(TokenLedger(incident_id="i1", priority="P0"))
    assert gw.complete("coordinator", PRIMARY_HARD_LIMIT + 1, 0)["status"] == "BUDGET_EXCEEDED"
    assert gw.complete("log", FAST_HARD_LIMIT + 1, 0)["status"] == "BUDGET_EXCEEDED"


def test_incident_budget_and_cost():
    gw = ModelGateway(TokenLedger(incident_id="i2", priority="P0"))
    r = gw.complete("coordinator", 26000, 3500)  # Problem-skill P0 input/output profile
    assert r["status"] == "ok"
    # A full P0 incident estimates to about $0.12 (Sec.3.3)
    assert 0.08 < estimate_p0_cost() < 0.16
    # Blow the budget
    gw2 = ModelGateway(TokenLedger(incident_id="i3", priority="P3"))
    gw2.complete("log", 20000, 2000)
    assert gw2.complete("log", 20000, 2000)["status"] == "BUDGET_EXCEEDED"


def test_frozen():
    ledger = TokenLedger(incident_id="i4")
    gw = ModelGateway(ledger)
    assert ledger.check_frozen("muse-spark-1.3", "xhigh", "gemini-3.8-flash", "medium")
    assert not ledger.check_frozen("muse-spark-1.3", "max", "gemini-3.8-flash", "medium")
