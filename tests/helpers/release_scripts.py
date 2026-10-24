"""A scripted bad-release incident over the lab's real recording (tests/fixtures/lab/bad-release).

The investigation finds the checkout rollout that moved the payment client to a dead port; the
planner's script is swappable, so tests can make it propose the right repair, the wrong one
(as if a log line had talked it into it), or nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.llm.types import LLMRequest
from app.tools.sources import Cassette
from tests.helpers.payment_scripts import finding, report
from tests.helpers.scripted_llm import evidence_ids

FIXTURES = Path(__file__).parent.parent / "fixtures" / "lab"
CITED = re.compile(r"(ev_[0-9a-f]{16}) \((\w+)\)")


def release_cassette() -> Cassette:
    return Cassette.load(FIXTURES / "bad-release.jsonl.gz")


def cited(text: str, tool: str | None = None) -> list[str]:
    """Evidence ids the task text cites, as "ev_... (tool)"."""
    return sorted({ev for ev, t in CITED.findall(text) if tool is None or t == tool})


Script = Callable[[LLMRequest, int], dict[str, Any]]


def undo(deployment: str = "checkout", tool: str = "rollout_history") -> Script:
    """The planner checks the rollout history itself, then proposes rolling ``deployment`` back."""

    def script(req: LLMRequest, n: int) -> dict[str, Any]:
        if n == 1:
            return {"actions": [{"tool": "rollout_history", "args": {"deployment": "checkout"}}]}
        ids = evidence_ids(req.turns, tool) or cited(req.turns[0]["text"], tool)
        return {
            "final": {
                "summary": f"Roll {deployment} back to the revision before the release.",
                "actions": [
                    {
                        "action": "rollout_undo",
                        "deployment": deployment,
                        "evidence_ids": ids[:2],
                        "reason": "revision 2 moved the payment client to port 9999",
                    }
                ],
                "alternatives": [{"action": "restart_deployment", "why_not": "a restart keeps the bad config"}],
            }
        }

    return script


def nothing(req: LLMRequest, n: int) -> dict[str, Any]:
    return {
        "final": {
            "summary": "Nothing in the catalogue fixes this.",
            "actions": [],
            "alternatives": [],
            "no_action_reason": "the cause is outside the catalogue; check it by hand",
        }
    }


def release_scripts(planner: Script | None = None) -> dict[str, Script]:
    def metrics(req: LLMRequest, n: int) -> dict[str, Any]:
        if n == 1:
            return {"actions": [{"tool": "prom_query", "args": {"query": "service:sli_error_ratio:rate5m"}}]}
        return {
            "final": report(
                finding("checkout error ratio 0.68", "metrics", evidence_ids(req.turns), service="checkout")
            )
        }

    def logs_traces(req: LLMRequest, n: int) -> dict[str, Any]:
        if n == 1:
            args = {"service": "checkout", "errors_only": True, "minutes": 10}
            return {"actions": [{"tool": "traces_search", "args": args}]}
        ids = evidence_ids(req.turns)
        return {"final": report(finding("PlaceOrder fails dialing :9999", "traces", ids, service="checkout"))}

    def changes(req: LLMRequest, n: int) -> dict[str, Any]:
        if n == 1:
            return {
                "actions": [
                    {"tool": "rollout_history", "args": {"deployment": "checkout"}},
                    {"tool": "deploy_diff", "args": {"deployment": "checkout"}},
                ]
            }
        return {
            "final": report(
                finding(
                    "revision 2 set PAYMENT_ADDR to payment:9999",
                    "changes",
                    evidence_ids(req.turns),
                    service="checkout",
                )
            )
        }

    def synthesizer(req: LLMRequest, n: int) -> dict[str, Any]:
        task = req.turns[0]["text"]
        change = cited(task, "rollout_history") + cited(task, "deploy_diff")
        return {
            "final": {
                "summary": "a checkout release broke its payment client",
                "root_causes": [
                    {
                        "service": "checkout",
                        "cause": "revision 2 points PAYMENT_ADDR at port 9999",
                        "confidence": 0.85,
                        "evidence_ids": change + cited(task, "traces_search"),
                    }
                ],
                "facts": [{"statement": "checkout fails 68% of requests", "evidence_ids": cited(task, "prom_query")}],
                "inferences": [],
                "hypotheses": [],
            }
        }

    return {
        "coordinator": lambda req, n: {
            "final": {
                "summary": "checkout burns its error budget",
                "hypotheses": ["a checkout change"],
                "briefs": {"metrics": "error ratios", "logs_traces": "checkout errors", "changes": "checkout rollouts"},
            }
        },
        "investigator.metrics": metrics,
        "investigator.logs_traces": logs_traces,
        "investigator.changes": changes,
        "synthesizer": synthesizer,
        "planner": planner or undo(),
    }
