"""Contract tests: whole investigations the real model ran on the lab, replayed.

``tests/fixtures/agent/<fault>/`` is what one lab run exported for that fault
(app/agents/recording.py): the incidents, every model step, every tool answer, and how each
investigation ended. Replaying it here runs the same prompts, loop, tools, summaries and
citation checks with no model and no cluster. Every request must hit the recording (the
request hash covers everything the model saw), and the outcome must be the recorded one.

So when a prompt, a tool summary or the loop's wording changes, these fail: import a new
lab run (copy lab-recordings/run-<id>/agent/* here) or, if the change was not meant to
change what the model sees, fix it.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app.agents import recording
from app.agents.investigation import AgentConfig

FIXTURES = Path(__file__).parent / "fixtures" / "agent"
FAULTS = sorted(p.name for p in FIXTURES.glob("*") if (p / "incidents.json").exists())

pytestmark = pytest.mark.skipif(not FAULTS, reason="no agent recordings imported yet")


def _cases() -> list[tuple[str, str]]:
    out = []
    for fault in FAULTS:
        doc = json.loads((FIXTURES / fault / "incidents.json").read_text())
        # Every incident whose investigation finished, whatever happened after (M5: planned, approved, ...).
        out += [
            (fault, inc)
            for inc, exp in sorted(doc["expected"].items())
            if exp["state"] not in ("RECEIVED", "TRIAGED", "INVESTIGATING")
        ]
    return out


@pytest.mark.parametrize(("fault", "incident"), _cases() or [("none", "none")])
async def test_a_recorded_lab_investigation_replays_to_the_same_outcome(engine, settings, fault, incident):
    rec = recording.Recording.load(FIXTURES / fault)
    expected = rec.expected[incident]
    await recording.load_incidents(engine, rec)
    config = AgentConfig(model_for=lambda role: "claude-haiku-5-5", max_steps=settings.agent_max_steps)
    outcome = await recording.replay(engine, rec, UUID(incident), config)

    async with engine.connect() as conn:
        misses = await conn.scalar(text("SELECT count(*) FROM tool_calls WHERE error_code = 'replay_miss'"))
    assert misses == 0
    assert {a: s for a, s in outcome.agents.items()} == expected["agents"]
    got = outcome.diagnosis.model_dump() if outcome.diagnosis else None
    assert got == expected["diagnosis"]

    # M5: the planner's proposal on that diagnosis, when the recording has one.
    if expected.get("plan") is not None:
        assert outcome.diagnosis is not None
        proposal = await recording.replay_plan(
            engine,
            rec,
            UUID(incident),
            outcome.diagnosis,
            model="claude-haiku-5-5",
            max_steps=settings.planner_max_steps,
        )
        assert proposal.plan is not None and proposal.plan.model_dump(mode="json") == expected["plan"]
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM tool_calls WHERE error_code = 'replay_miss'")) == 0
