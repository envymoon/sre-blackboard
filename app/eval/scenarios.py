"""Labelled fault scenarios (``eval/scenarios/*.yaml``).

A scenario says what the lab injects and what the right answer is. The labels are written
by hand from the injection alone, before and independently of any run: the root cause is
the service the fault was put into, the repair is what a human SRE would approve for that
fault. Nothing here is derived from what the system answered (T5 in the plan: the first
version's evaluation produced its expected answers with the code it was testing).

Faults are named from the lab's catalogue (scripts/lab/faults.py); a test checks that every
name exists and that the labels agree with the catalogue's.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.remediation.actions import ActionKind

SCENARIOS = Path(__file__).resolve().parents[2] / "eval" / "scenarios"
Difficulty = Literal["easy", "medium", "hard"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Act(_Model):
    action: ActionKind
    deployment: str

    def __str__(self) -> str:
        return f"{self.action} {self.deployment}"


class Injection(_Model):
    fault: str = Field(description="A name from the lab's fault catalogue")
    after_s: float = Field(0, ge=0, description="Seconds to wait after the previous injection")
    # cause: the fault being diagnosed. distractor: something else that happens around it,
    # a harmless rollout or an unrelated fault with its own alerts.
    role: Literal["cause", "distractor"] = "cause"
    # A distractor that alerts has its own right answer for its own incidents.
    root_cause: str | None = None
    symptoms: tuple[str, ...] = ()


class Expected(_Model):
    root_cause: str = Field(description="The service the fault is in: the one to fix")
    fault_type: str
    # Services expected to alert (event hosts), to tell this fault's incidents from a distractor's.
    symptoms: tuple[str, ...]
    # What a human should approve. Empty: nothing in the action catalogue repairs this fault.
    repair: tuple[Act, ...] = ()
    # Not the repair, but not wrong either: a mitigation on the root cause (restart a leaking
    # service, scale a saturated one).
    acceptable: tuple[Act, ...] = ()


class Scenario(_Model):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str
    difficulty: Difficulty
    tags: tuple[str, ...] = ()
    inject: tuple[Injection, ...] = Field(min_length=1)
    expect: Expected
    notes: str = ""

    @model_validator(mode="after")
    def _one_cause(self) -> Scenario:
        causes = [i for i in self.inject if i.role == "cause"]
        if len(causes) != 1:
            raise ValueError(f"{self.id}: exactly one injection must be the cause, not {len(causes)}")
        for a in (*self.expect.repair, *self.expect.acceptable):
            if a.deployment != self.expect.root_cause:
                raise ValueError(f"{self.id}: {a} is not on the root cause {self.expect.root_cause}")
        return self

    @property
    def cause(self) -> Injection:
        return next(i for i in self.inject if i.role == "cause")

    def expected_root_cause(self, incident_service: str) -> str:
        """The right answer for one incident: a distractor's own cause for its own alerts."""
        if incident_service not in self.expect.symptoms:
            for d in self.inject:
                if d.role == "distractor" and d.root_cause and incident_service in d.symptoms:
                    return d.root_cause
        return self.expect.root_cause

    def allowed(self) -> frozenset[str]:
        """Actions that are not wrong, as "action deployment"."""
        return frozenset(str(a) for a in (*self.expect.repair, *self.expect.acceptable))

    def harmless(self, deployment: str) -> bool:
        """Not a safety problem: acting on the root cause (or a distractor's own cause) at most
        fails to help. Changing anything else changes a healthy service."""
        causes = {self.expect.root_cause} | {
            d.root_cause for d in self.inject if d.role == "distractor" and d.root_cause
        }
        return deployment in causes


def load(path: Path) -> Scenario:
    scenario = Scenario.model_validate(yaml.safe_load(path.read_text()))
    if scenario.id != path.stem:
        raise ValueError(f"{path.name}: id {scenario.id!r} does not match the file name")
    return scenario


@cache
def catalogue(directory: Path = SCENARIOS) -> tuple[Scenario, ...]:
    return tuple(load(p) for p in sorted(directory.glob("*.yaml")))


def get(scenario_id: str, directory: Path = SCENARIOS) -> Scenario:
    for s in catalogue(directory):
        if s.id == scenario_id:
            return s
    raise KeyError(f"unknown scenario {scenario_id!r}")
