"""The labelled scenarios (eval/scenarios) against the lab's fault catalogue."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.eval import scenarios
from app.eval.scenarios import Scenario
from scripts.lab import faults

CATALOGUE = scenarios.catalogue()


def test_there_are_20_to_30_scenarios_across_kinds_and_difficulties() -> None:
    assert 20 <= len(CATALOGUE) <= 30
    assert {s.difficulty for s in CATALOGUE} == {"easy", "medium", "hard"}
    kinds = {faults.get(s.cause.fault).kind for s in CATALOGUE}
    assert kinds == {"flag", "chaos", "release"}
    assert sum(1 for s in CATALOGUE if any(i.role == "distractor" for i in s.inject)) >= 4
    assert sum(1 for s in CATALOGUE if s.expect.repair) >= 4


@pytest.mark.parametrize("scenario", CATALOGUE, ids=lambda s: s.id)
def test_labels_agree_with_the_fault_catalogue(scenario: Scenario) -> None:
    cause = faults.get(scenario.cause.fault)
    assert scenario.expect.root_cause == cause.root_cause
    assert scenario.expect.fault_type == cause.fault_type
    # The catalogue's known repair is the scenario's (the acceptance run approves exactly it).
    assert [(a.action, a.deployment) for a in scenario.expect.repair] == ([cause.repair] if cause.repair else [])
    for i in scenario.inject:
        fault = faults.get(i.fault)
        assert "benign" not in fault.tags or i.role == "distractor"
        if i.role == "distractor" and i.root_cause is not None:
            assert i.root_cause == fault.root_cause and i.symptoms
    released = [faults.get(i.fault).mechanism for i in scenario.inject]
    deployments = [m.deployment for m in released if isinstance(m, faults.ReleaseFault)]
    assert len(deployments) == len(set(deployments)), "two releases of one Deployment cannot be undone apart"


def test_each_incident_is_scored_against_its_own_fault() -> None:
    s = scenarios.get("network-delay-with-ad-failure")
    assert s.expected_root_cause("ad") == "ad"
    assert s.expected_root_cause("frontend") == "product-catalog"
    assert s.expected_root_cause("somewhere-else") == "product-catalog"
    assert s.harmless("ad") and not s.harmless("frontend")


def test_a_distractor_release_is_not_an_allowed_action() -> None:
    s = scenarios.get("payment-failure-after-checkout-release")
    assert s.allowed() == frozenset()
    assert not s.harmless("checkout")
    assert scenarios.get("bad-release").allowed() == {"rollout_undo checkout"}


def test_bad_scenarios_are_refused(tmp_path: Path) -> None:
    base = yaml.safe_load((scenarios.SCENARIOS / "bad-release.yaml").read_text())
    with pytest.raises(ValidationError, match="exactly one injection must be the cause"):
        Scenario.model_validate({**base, "inject": [{"fault": "bad-release", "role": "distractor"}]})
    repair_elsewhere = {**base["expect"], "repair": [{"action": "rollout_undo", "deployment": "payment"}]}
    with pytest.raises(ValidationError, match="not on the root cause"):
        Scenario.model_validate({**base, "expect": repair_elsewhere})
    with pytest.raises(ValidationError, match="restart_everything"):
        Scenario.model_validate(
            {
                **base,
                "expect": {**base["expect"], "repair": [{"action": "restart_everything", "deployment": "checkout"}]},
            }
        )
    (tmp_path / "other-name.yaml").write_text(yaml.safe_dump(base))
    with pytest.raises(ValueError, match="does not match the file name"):
        scenarios.load(tmp_path / "other-name.yaml")
