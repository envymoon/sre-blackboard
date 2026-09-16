"""End-to-end test: unbroken P0 chain. Maps to spec Sec.27.2."""
import json, pathlib
from app.e2e import run_incident_e2e


def test_e2e_p0_unbroken():
    alert = json.loads(pathlib.Path("fixtures/p0_baseline.json").read_text(encoding="utf-8"))
    out = run_incident_e2e(alert)
    assert out["state"].stage == "resolve_or_escalate"
    assert len(out["state"].artifacts) >= 1
    assert out["report"]["recommended_action"]["risk_level"] == "R3"
    assert out["ledger"].input_used > 0 and out["approval"]["approved"]
    assert len(out["audit"]) >= 3
    assert out["verification"]["burn"] == "stopped"
