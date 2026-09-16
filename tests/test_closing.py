"""Closing tests: main entrypoint / gateway inside Harness / approval singleton / synthesis."""
from app.main import app as main_app
from app.api import app as api_app
from app.auth import APPROVALS
from app.agents import synthesize
from app.models import Artifact


def test_main_entrypoint():
    assert main_app is api_app and main_app is not None


def test_approval_singleton_roundtrip():
    APPROVALS.create_card("inc-web", "AP-W", "P0", "checkout-service", "a", "b", ["E1"], "x", expires_s=3600)
    assert APPROVALS.approve("inc-web", "AP-W", "owner", "k-web")["approved"]


def test_gateway_in_harness_and_synthesize():
    from app.harness import AgentHarness
    from app.runtime import AgentRuntime
    from app.gateway import ModelGateway, TokenLedger
    from app.models import Task, IncidentState
    from app.agents import StubModel
    h = AgentHarness(runtime=AgentRuntime())
    gw = ModelGateway(TokenLedger(incident_id="i-gw", priority="P3"))
    gw.complete("log", 20000, 2000)
    m = StubModel("gemini-3.8-flash")
    m.agent_role = "log"
    t = Task(task_id="t1", incident_id="i-gw", alert_type="Problem", priority="P3", capability="logs")
    # P3 budget nearly exhausted; a large call must escalate to a human
    assert h.run_task(t, IncidentState(incident_id="i-gw"), m, gateway=gw, input_tokens=20000, output_tokens=2000) == "ESCALATE_TO_HUMAN"
    assert any(e.get("model_id") == "gemini-3.8-flash" for e in h.tracer.events)
    a1 = Artifact(incident_id="i", producer="p", artifact_type="Log", facts=[], evidence_refs=["E1", "E2"], hypotheses=[], gaps=[], next_task_proposals=[])
    a2 = Artifact(incident_id="i", producer="p", artifact_type="Trace", facts=[], evidence_refs=["E1"], hypotheses=[], gaps=[], next_task_proposals=[])
    best, gaps = synthesize([a1, a2])
    assert best is a1 and any("Trace" in g for g in gaps)
