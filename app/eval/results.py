"""Result files: the observations of one evaluation run, as JSON (eval/results/*.json).

Only observations are stored (what each incident concluded, what was proposed, what it
cost), never scores, so a change to the scoring rescores every past run the same way.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.eval import scenarios
from app.eval.score import ScenarioObs, ScenarioScore, score

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "eval" / "baseline.json"


class ResultFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: str = Field(description="The Actions run id, or a name")
    kind: str = Field(description="live (lab and model) or replay (recordings)")
    commit: str = ""
    observations: list[ScenarioObs] = Field(default_factory=list)

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(mode="json"), indent=1, sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: Path) -> ResultFile:
        return cls.model_validate_json(path.read_text())


def score_all(observations: Iterable[ScenarioObs], directory: Path = scenarios.SCENARIOS) -> list[ScenarioScore]:
    return [score(scenarios.get(o.scenario, directory), o) for o in observations]


def load_scores(paths: Sequence[Path]) -> list[ScenarioScore]:
    out: list[ScenarioScore] = []
    for p in paths:
        f = ResultFile.load(p)
        out += score_all(o if o.run else o.model_copy(update={"run": f.run}) for o in f.observations)
    return out


def baseline_files(path: Path = BASELINE) -> list[Path]:
    """The committed runs the gate compares against: {"variant": "default", "results": [...]}."""
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    return [REPO / r for r in doc["results"]]
