"""A scripted payment-failure investigation over the real lab recording (tests/fixtures/lab).

Every agent asks for data the lab really returned during the fault and cites the evidence ids
the harness handed back, so the tests exercise real tool code, real evidence and the real
citation check; only the model is scripted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.llm.types import LLMRequest
from app.tools.sources import Cassette
from tests.helpers.scripted_llm import evidence_ids

FIXTURES = Path(__file__).parent.parent / "fixtures" / "lab"
EV = re.compile(r"ev_[0-9a-f]{16}")


def payment_cassette() -> Cassette:
    return Cassette.load(FIXTURES / "payment-failure.jsonl.gz")


ERROR_RATIO = {"query": "service:sli_error_ratio:rate5m"}
ERROR_TREND = {"query": "service:sli_error_ratio:rate5m", "minutes": 15}
PAYMENT_LOGS = {"service": "payment", "level": "warn", "minutes": 10}
PAYMENT_TRACES = {"service": "payment", "errors_only": True, "minutes": 10}


def finding(
    statement: str, signal: str, ids: list[str], kind: str = "fact", service: str | None = "payment"
) -> dict[str, Any]:
    return {
        "statement": statement,
        "kind": kind,
        "signal": signal,
        "service": service,
        "evidence_ids": ids,
        "confidence": 0.9,
    }


def report(*findings: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "scripted", "findings": list(findings)}


def metrics_script(req: LLMRequest, n: int) -> dict[str, Any]:
    if n == 1:
        return {"actions": [{"tool": "prom_query", "args": ERROR_RATIO}, {"tool": "prom_range", "args": ERROR_TREND}]}
    ids = evidence_ids(req.turns, "prom_query")
    return {"final": report(finding("payment error ratio 0.39", "metrics", ids))}


def payment_scripts() -> dict[str, Any]:
    def logs_traces(req: LLMRequest, n: int) -> dict[str, Any]:
        if n == 1:
            return {
                "actions": [
                    {"tool": "logs_search", "args": PAYMENT_LOGS},
                    {"tool": "traces_search", "args": PAYMENT_TRACES},
                ]
            }
        return {
            "final": report(
                finding("payment logs 'Invalid token'", "logs", evidence_ids(req.turns, "logs_search")),
                finding("charge spans fail in payment", "traces", evidence_ids(req.turns, "traces_search")),
            )
        }

    def changes(req: LLMRequest, n: int) -> dict[str, Any]:
        if n == 1:
            return {"actions": [{"tool": "flag_state", "args": {}}]}
        return {"final": report(finding("flag paymentFailure is on", "flags", evidence_ids(req.turns)))}

    def synthesizer(req: LLMRequest, n: int) -> dict[str, Any]:
        ids = sorted(set(EV.findall(req.turns[0]["text"])))
        return {
            "final": {
                "summary": "payment rejects charges",
                "root_causes": [
                    {"service": "payment", "cause": "paymentFailure flag", "confidence": 0.9, "evidence_ids": ids[:2]}
                ],
                "facts": [{"statement": "payment errors", "evidence_ids": ids[:1]}],
                "inferences": [],
                "hypotheses": [{"statement": "a bad deploy", "how_to_verify": "check rollouts"}],
            }
        }

    return {
        "coordinator": lambda req, n: {
            "final": {
                "summary": "checkout and payment burn their error budget",
                "hypotheses": ["payment failing"],
                "briefs": {"metrics": "error ratios", "logs_traces": "payment errors", "changes": "flags and rollouts"},
            }
        },
        "investigator.metrics": metrics_script,
        "investigator.logs_traces": logs_traces,
        "investigator.changes": changes,
        "synthesizer": synthesizer,
    }
