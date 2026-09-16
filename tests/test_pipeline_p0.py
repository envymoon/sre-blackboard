"""Pipeline smoke test: minimal driver of the fixed 10-stage workflow. Maps to spec Sec.8.5/Sec.13.5."""
import json, pathlib
from app.harness import AgentHarness
from app.runtime import AgentRuntime
from app.pipeline import run_p0_problem_pipeline
from app.safety import safety_review


def test_p0_pipeline_stages():
    alert = json.loads(pathlib.Path("fixtures/p0_baseline.json").read_text(encoding="utf-8"))
    harness = AgentHarness(runtime=AgentRuntime())
    state = run_p0_problem_pipeline(alert, harness)
    assert state.stage == "safety_review"
    assert set(state.tasks) == {"t-1", "t-2", "t-3", "t-4"}
    assert len(state.artifacts) >= 1
    v = safety_review(state.artifacts[0], {"name": "rollback_release"})
    assert v["risk"] == "R3" and v["verdict"] == "NEED_HUMAN_APPROVAL"
