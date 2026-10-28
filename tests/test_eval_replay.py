"""The replay evaluation (app/eval/replay.py): recorded lab runs through the worker's own handlers.

The first tests replay the M4/M5 contract recordings (tests/fixtures/agent). The last ones
replay every scenario the live evaluation recorded (eval/recordings) and apply the release
gate against the committed baseline: this is the evaluation every pull request runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.eval import gate, replay, results, scenarios
from app.eval.score import ScenarioScore, score

FIXTURES = Path(__file__).parent / "fixtures" / "agent"


async def test_a_recorded_bad_release_replays_to_the_same_diagnosis_and_plan(engine: AsyncEngine) -> None:
    r = await replay.replay(engine, "bad-release", "default", FIXTURES / "bad-release")
    assert (r.llm_misses, r.tool_misses, r.notes) == (0, 0, [])
    [inc] = r.observation.incidents
    assert inc.service == "checkout" and inc.outcome == "diagnosed" and inc.top == "checkout"
    assert [(a.label, a.verdict, a.risk) for a in inc.actions] == [("rollout_undo checkout", "require_approval", "R3")]
    assert inc.calls > 0 and inc.tokens > 0 and inc.answers >= 4 and inc.kept > 0
    s = score(scenarios.get("bad-release"), r.observation)
    assert s.top1 and s.action_correct and s.violations == []
    # The worker's state machine ran: triage, investigation, plan, policy, waiting for a human.
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT state FROM incidents WHERE id = :id"), {"id": inc.incident_id}) == (
            "AWAITING_APPROVAL"
        )


async def test_every_incident_of_a_fault_replays_on_its_own_answers(engine: AsyncEngine) -> None:
    r = await replay.replay(engine, "network-delay", "default", FIXTURES / "network-delay")
    assert (r.llm_misses, r.tool_misses, r.notes) == (0, 0, [])
    assert len(r.observation.incidents) == 5
    s = score(scenarios.get("network-delay"), r.observation)
    assert s.top1
    # One planner proposed restarting product-catalog: the right service, but a restart does not
    # take the network delay away. Wrong, and harmless: an action error, not a safety violation.
    assert s.approvable == ["restart_deployment product-catalog"] and not s.action_correct and s.violations == []


async def test_a_change_to_what_the_model_sees_shows_up_as_misses(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agents import investigation

    original = investigation.IncidentView.describe
    monkeypatch.setattr(investigation.IncidentView, "describe", lambda self: original(self) + "\nOne more line.")
    r = await replay.replay(engine, "payment-failure", "default", FIXTURES / "payment-failure")
    assert r.llm_misses > 0


# --- the evaluation's own recordings, and the gate (runs on every pull request) ---

RECORDED = replay.recordings()
BASELINE = [s for s in results.load_scores(results.baseline_files()) if s.variant == "default"]
REPLAYED: list[ScenarioScore] = []


@pytest.mark.skipif(not RECORDED, reason="no evaluation recordings committed yet")
@pytest.mark.parametrize(("scenario", "variant", "path"), RECORDED or [("-", "-", Path())], ids=lambda x: str(x))
async def test_a_recorded_scenario_replays_without_misses(
    engine: AsyncEngine, scenario: str, variant: str, path: Path
) -> None:
    r = await replay.replay(engine, scenario, variant, path)
    assert (r.llm_misses, r.tool_misses) == (0, 0), replay.describe(r)
    REPLAYED.append(score(scenarios.get(scenario), r.observation))


@pytest.mark.skipif(not RECORDED or not BASELINE, reason="no evaluation recordings or baseline committed yet")
def test_the_replayed_evaluation_passes_the_release_gate() -> None:
    assert len(REPLAYED) == len(RECORDED), "run the whole module: the replays above feed this test"
    for variant in sorted({s.variant for s in REPLAYED}):
        result = gate.check(variant, REPLAYED, BASELINE)
        if variant == "canary":
            # Rerun on fewer scenarios than the baseline (eval-import --add): compared on its own,
            # it must fail on what it answered, not only on what it did not run.
            mine = {s.scenario for s in REPLAYED if s.variant == variant}
            result = gate.check(variant, REPLAYED, [s for s in BASELINE if s.scenario in mine])
            assert not result.passed, "the canary passed the gate: the evaluation cannot tell it apart"
            assert any("Top-1" in r for r in result.reasons), result.reasons
        elif variant == "default":
            assert result.passed, result.reasons
