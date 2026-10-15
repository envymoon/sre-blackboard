"""Agent loop, citation checks, the investigation flow and its checkpoints, the report.

The model is scripted (tests/helpers/scripted_llm.py); the tools replay what the real lab
returned during the payment-failure fault (tests/fixtures/lab), so the harness runs real
tool code on real data.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.agents import prompts
from app.agents.citations import check_diagnosis, check_report
from app.agents.investigation import AgentConfig, IncidentView, investigate
from app.agents.loop import LAST_STEP_NOTE, AgentFailed, AgentSpec, BudgetExceeded, Deps, run_agent
from app.agents.report import ReportInput, render_markdown
from app.agents.schemas import Diagnosis, Finding, InvestigatorReport
from app.agents.store import MemoryAgentStore
from app.llm.types import LLMError, LLMRequest
from app.tools import ALL_TOOLS, ToolRegistry
from app.tools.sources import ReplaySource
from tests.helpers.payment_scripts import (
    ERROR_RATIO,
    finding,
    metrics_script,
    payment_cassette,
    payment_scripts,
    report,
)
from tests.helpers.scripted_llm import ScriptedLLM, evidence_ids

FIXTURES = Path(__file__).parent / "fixtures" / "lab"
INCIDENT = UUID("00000000-0000-4000-8000-000000000001")
CASSETTE = payment_cassette()


def deps(llm: ScriptedLLM, store: MemoryAgentStore, *, budget: int = 1_000_000) -> Deps:
    registry = ToolRegistry(ALL_TOOLS, ReplaySource(CASSETTE), store)
    return Deps(llm, registry, store, "lab", INCIDENT, budget)


METRICS_SPEC = AgentSpec(
    "investigator.metrics",
    prompts.investigator("metrics"),
    ("prom_query", "prom_range"),
    InvestigatorReport,
    max_steps=4,
)


async def _check(r: InvestigatorReport, shown: dict[str, str], store: MemoryAgentStore) -> Any:
    cited = {ev for f in r.findings for ev in f.evidence_ids}
    return check_report(r, await store.evidence_tools(INCIDENT, cited), shown)


def run(spec: AgentSpec, llm: ScriptedLLM, store: MemoryAgentStore, **kw: Any) -> Any:
    async def check(r: InvestigatorReport, shown: dict[str, str]) -> Any:
        return await _check(r, shown, store)

    return asyncio.run(
        run_agent(spec, "Alert: ErrorBudgetBurn on checkout.", model="m", deps=deps(llm, store, **kw), check=check)
    )


# --- the loop ---


def test_loop_runs_the_requested_tools_and_accepts_a_cited_answer() -> None:
    store = MemoryAgentStore()
    llm = ScriptedLLM({"investigator.metrics": metrics_script})
    out = run(METRICS_SPEC, llm, store)
    assert out.steps == 2 and out.tool_calls == 2 and out.rewrites == 0 and out.dropped == 0
    assert isinstance(out.final, InvestigatorReport) and out.final.findings[0].evidence_ids[0].startswith("ev_")
    assert set(out.shown.values()) == {"prom_query", "prom_range"}
    # The model saw summaries with evidence ids, in the turn after its request.
    first, second = llm.requests
    assert [t["type"] for t in second.turns] == ["user", "step", "results"]
    assert second.turns[1]["actions"][0] == {"id": "c1_1", "tool": "prom_query", "args": ERROR_RATIO}
    # Ledger: one row per step, with the model's step stored for replay.
    assert [c.step_no for c in store.calls] == [1, 2] and all(c.status == "ok" for c in store.calls)
    assert store.calls[0].request_hash == first.hash() and store.calls[0].step is not None
    assert {c.agent for c in store.tool_calls} == {"investigator.metrics"}


def test_a_made_up_evidence_id_goes_back_once_then_the_finding_is_dropped() -> None:
    def script(req: LLMRequest, n: int) -> dict[str, Any]:
        if n == 1:
            return {"actions": [{"tool": "prom_query", "args": ERROR_RATIO}]}
        real = evidence_ids(req.turns)
        return {
            "final": report(
                finding("real", "metrics", real),
                finding("invented", "metrics", ["ev_00000000deadbeef"]),
            )
        }

    store = MemoryAgentStore()
    llm = ScriptedLLM({"investigator.metrics": script})
    out = run(METRICS_SPEC, llm, store)
    assert out.rewrites == 1 and out.dropped == 1
    assert [f.statement for f in out.final.findings] == ["real"]
    feedback = llm.requests[-1].turns[-1]
    assert (
        feedback["type"] == "user" and "ev_00000000deadbeef is not an evidence id of this incident" in feedback["text"]
    )


def test_a_fixed_answer_after_feedback_is_kept_whole() -> None:
    def script(req: LLMRequest, n: int) -> dict[str, Any]:
        if n == 1:
            return {"actions": [{"tool": "prom_query", "args": ERROR_RATIO}]}
        ids = evidence_ids(req.turns)
        # First final claims a log finding from a metrics query; after feedback, fixes the signal.
        signal = "logs" if n == 2 else "metrics"
        return {"final": report(finding("payment errors", signal, ids))}

    out = run(METRICS_SPEC, ScriptedLLM({"investigator.metrics": script}), MemoryAgentStore())
    assert out.rewrites == 1 and out.dropped == 0 and out.final.findings[0].signal == "metrics"


def test_a_tool_outside_the_role_is_refused_not_run() -> None:
    def script(req: LLMRequest, n: int) -> dict[str, Any]:
        if n == 1:
            return {"actions": [{"tool": "flag_state", "args": {}}]}
        return {"final": report()}

    store = MemoryAgentStore()
    llm = ScriptedLLM({"investigator.metrics": script})
    out = run(METRICS_SPEC, llm, store)
    assert out.tool_errors == 1 and store.tool_calls == []
    assert "not one of your data sources" in llm.requests[1].turns[-1]["results"][0]["content"]


def test_the_last_step_must_be_the_final_answer() -> None:
    def script(req: LLMRequest, n: int) -> dict[str, Any]:
        if req.force_final:
            return {"final": report()}
        return {"actions": [{"tool": "prom_query", "args": ERROR_RATIO}]}

    llm = ScriptedLLM({"investigator.metrics": script})
    spec = AgentSpec(METRICS_SPEC.name, METRICS_SPEC.prompt, METRICS_SPEC.tools, InvestigatorReport, max_steps=3)
    out = run(spec, llm, MemoryAgentStore())
    assert out.steps == 3 and [r.force_final for r in llm.requests] == [False, False, True]
    assert llm.requests[2].turns[-1] == {"type": "user", "text": LAST_STEP_NOTE}


def test_an_answer_that_never_fits_the_schema_fails_the_agent() -> None:
    llm = ScriptedLLM({"investigator.metrics": lambda req, n: {"final": {"summary": "x"}}})
    with pytest.raises(AgentFailed):
        run(METRICS_SPEC, llm, MemoryAgentStore())
    assert "findings: Field required" in llm.requests[1].turns[-1]["text"]


def test_the_budget_is_checked_before_every_step() -> None:
    llm = ScriptedLLM({"investigator.metrics": metrics_script}, tokens_per_step=600)
    with pytest.raises(BudgetExceeded):
        run(METRICS_SPEC, llm, MemoryAgentStore(), budget=500)
    assert len(llm.requests) == 1


# --- citation checks ---


def test_citation_rules() -> None:
    known = {"ev_a": "prom_query", "ev_b": "logs_search", "ev_c": "flag_state"}
    shown = {"ev_a", "ev_b"}
    r = InvestigatorReport.model_validate(
        report(
            finding("ok", "metrics", ["ev_a"]),
            finding("wrong signal", "traces", ["ev_a"]),
            finding("not shown to this agent", "flags", ["ev_c"]),
            finding("unknown", "metrics", ["ev_zzz"]),
            finding("fact without evidence", "logs", []),
            finding("hypothesis without evidence is fine", "logs", [], kind="hypothesis"),
        )
    )
    checked = check_report(r, known, shown)
    assert [f.statement for f in checked.value.findings] == ["ok", "hypothesis without evidence is fine"]
    assert checked.dropped == 4 and len(checked.problems) == 4


def test_the_diagnosis_may_only_cite_what_the_findings_cite() -> None:
    dx = Diagnosis.model_validate(
        {
            "summary": "s",
            "root_causes": [
                {"service": "payment", "cause": "c", "confidence": 0.9, "evidence_ids": ["ev_a"]},
                {"service": "checkout", "cause": "c", "confidence": 0.2, "evidence_ids": ["ev_c"]},
            ],
            "facts": [{"statement": "f", "evidence_ids": ["ev_a", "ev_b"]}],
            "inferences": [],
            "hypotheses": [],
        }
    )
    checked = check_diagnosis(dx, {"ev_a": "prom_query", "ev_b": "logs_search", "ev_c": "flag_state"}, {"ev_a", "ev_b"})
    assert [r.service for r in checked.value.root_causes] == ["payment"] and checked.dropped == 1


# --- the whole investigation ---


def incident() -> IncidentView:
    return IncidentView(
        id=INCIDENT,
        tenant_id="lab",
        title="ErrorBudgetBurn",
        service="checkout",
        environment="lab",
        priority="P1",
        created_at=datetime(2026, 10, 15, 4, 5, tzinfo=UTC),
        labels={"alertname": "ErrorBudgetBurn", "service_name": "checkout", "burn": "fast"},
        related=[
            {
                "title": "ErrorBudgetBurn",
                "service": "payment",
                "priority": "P1",
                "created_at": datetime(2026, 10, 15, 4, 5, tzinfo=UTC),
            }
        ],
    )


CONFIG = AgentConfig(model_for=lambda role: "claude-haiku-5-5", max_steps=4)


def test_investigation_runs_every_agent_and_ends_with_a_cited_diagnosis() -> None:
    store = MemoryAgentStore()
    llm = ScriptedLLM(payment_scripts())
    outcome = asyncio.run(investigate(incident(), deps(llm, store), CONFIG))
    assert outcome.escalate is None and outcome.diagnosis is not None
    assert outcome.diagnosis.root_causes[0].service == "payment"
    assert set(outcome.agents.values()) == {"done"} and len(outcome.agents) == 5
    kinds = [(e.kind, e.author) for e in store.board[INCIDENT]]
    assert kinds.count(("finding", "investigator.logs_traces")) == 2 and ("diagnosis", "synthesizer") in kinds
    # Each investigator got the coordinator's brief; the synthesizer got every finding.
    metrics_task = next(r for r in llm.requests if r.agent == "investigator.metrics").turns[0]["text"]
    assert "Your brief: error ratios" in metrics_task and "ErrorBudgetBurn on payment (P1)" in metrics_task
    synth_task = next(r for r in llm.requests if r.agent == "synthesizer").turns[0]["text"]
    assert "flag paymentFailure is on" in synth_task and "(flag_state)" in synth_task
    assert all(
        r.prompt_version == run.prompt_version for r in llm.requests for run in [store.run_rows[(INCIDENT, r.agent)]]
    )


def test_a_rerun_after_a_crash_does_not_ask_finished_agents_again() -> None:
    store = MemoryAgentStore()
    llm = ScriptedLLM(payment_scripts())
    llm.fail["synthesizer"] = LLMError("rate_limited", "slow down", retryable=True)
    with pytest.raises(LLMError):
        asyncio.run(investigate(incident(), deps(llm, store), CONFIG))
    assert {a: r.status for (_, a), r in store.run_rows.items()}["synthesizer"] == "failed"
    before = {a: llm.calls(a) for a in payment_scripts()}

    del llm.fail["synthesizer"]
    outcome = asyncio.run(investigate(incident(), deps(llm, store), CONFIG))
    assert outcome.diagnosis is not None
    assert {a: llm.calls(a) - before[a] for a in before} == {
        "coordinator": 0,
        "investigator.metrics": 0,
        "investigator.logs_traces": 0,
        "investigator.changes": 0,
        "synthesizer": 1,
    }
    assert outcome.agents["investigator.metrics"] == "reused"
    # The reused findings are not written twice.
    assert sum(1 for e in store.board[INCIDENT] if e.kind == "finding") == 4


def test_a_failing_investigator_does_not_stop_the_others() -> None:
    llm = ScriptedLLM(payment_scripts())
    llm.fail["investigator.changes"] = LLMError("bad_request", "context too long")
    outcome = asyncio.run(investigate(incident(), deps(llm, MemoryAgentStore()), CONFIG))
    assert outcome.agents["investigator.changes"] == "failed" and outcome.diagnosis is not None


def test_running_out_of_budget_escalates() -> None:
    llm = ScriptedLLM(payment_scripts(), tokens_per_step=5000)
    outcome = asyncio.run(investigate(incident(), deps(llm, MemoryAgentStore(), budget=12_000), CONFIG))
    assert outcome.diagnosis is None and outcome.escalate is not None and "budget" in outcome.escalate


def test_no_supported_root_cause_escalates() -> None:
    scripts = payment_scripts()
    scripts["synthesizer"] = lambda req, n: {
        "final": {
            "summary": "unclear",
            "root_causes": [
                {"service": "payment", "cause": "guess", "confidence": 0.3, "evidence_ids": ["ev_00000000deadbeef"]}
            ],
            "facts": [],
            "inferences": [],
            "hypotheses": [],
        }
    }
    outcome = asyncio.run(investigate(incident(), deps(ScriptedLLM(scripts), MemoryAgentStore()), CONFIG))
    assert outcome.escalate == "no root cause candidate is supported by the evidence"


# --- report ---


def test_report_keeps_facts_inferences_and_hypotheses_apart_and_links_evidence() -> None:
    store = MemoryAgentStore()
    outcome = asyncio.run(investigate(incident(), deps(ScriptedLLM(payment_scripts()), store), CONFIG))
    ev = outcome.diagnosis.root_causes[0].evidence_ids[0]
    md = render_markdown(
        ReportInput(
            incident={
                "id": INCIDENT,
                "title": "ErrorBudgetBurn",
                "service": "checkout",
                "priority": "P1",
                "state": "DIAGNOSED",
                "environment": "lab",
                "created_at": datetime(2026, 10, 15, 4, 5, tzinfo=UTC),
            },
            diagnosis=outcome.diagnosis,
            findings=[(e.author, Finding.model_validate(e.body)) for e in store.board[INCIDENT] if e.kind == "finding"],
            evidence={
                ev: {
                    "tool": "prom_query",
                    "args": ERROR_RATIO,
                    "result_hash": "ab" * 32,
                    "fetched_at": datetime.now(UTC),
                    "source": "replay",
                }
            },
            runs={a: r for (_, a), r in store.run_rows.items()},
            usage={"calls": 9, "tokens": 9000, "cost_usd": 0.009},
        )
    )
    for heading in (
        "## Root cause candidates",
        "## Facts",
        "## High-confidence inferences",
        "## Hypotheses to verify",
        "## Evidence",
    ):
        assert heading in md
    assert f"| 1 | payment | paymentFailure flag | 90% | [{ev}](/api/v1/evidence/{ev})" in md
    assert "equivalent API cost: $0.0090" in md
