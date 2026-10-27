"""The evaluation's agent variants (app/agents/variants.py): same tools and checks, different shape."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents import prompts, variants
from app.agents.investigation import INVESTIGATOR_TOOLS, SOLO, AgentConfig, investigate
from app.agents.store import MemoryAgentStore
from app.llm.types import LLMRequest
from tests.helpers.payment_scripts import ERROR_RATIO, PAYMENT_LOGS, payment_scripts
from tests.helpers.scripted_llm import ScriptedLLM, evidence_ids
from tests.test_agents import INCIDENT, deps, incident


def config(name: str, max_steps: int = 4) -> AgentConfig:
    return AgentConfig(model_for=lambda role: "claude-haiku-5-5", max_steps=max_steps, variant=variants.get(name))


def solo_script(req: LLMRequest, n: int) -> dict[str, Any]:
    if n == 1:
        return {
            "actions": [
                {"tool": "prom_query", "args": ERROR_RATIO},
                {"tool": "logs_search", "args": PAYMENT_LOGS},
                {"tool": "flag_state", "args": {}},
            ]
        }
    ids = evidence_ids(req.turns)
    return {
        "final": {
            "summary": "payment rejects charges",
            "root_causes": [
                {"service": "payment", "cause": "paymentFailure flag", "confidence": 0.8, "evidence_ids": ids[:2]},
                # Not shown to this agent: dropped by the citation check after one rewrite.
                {"service": "cart", "cause": "guess", "confidence": 0.1, "evidence_ids": ["ev_00000000deadbeef"]},
            ],
            "facts": [{"statement": "payment errors", "evidence_ids": ids[:1]}],
            "inferences": [],
            "hypotheses": [],
        }
    }


def test_the_single_agent_variant_has_every_tool_and_writes_the_diagnosis_itself() -> None:
    store = MemoryAgentStore()
    llm = ScriptedLLM({SOLO: solo_script})
    outcome = asyncio.run(investigate(incident(), deps(llm, store), config("single")))

    assert outcome.escalate is None and outcome.diagnosis is not None
    assert [r.service for r in outcome.diagnosis.root_causes] == ["payment"]
    assert outcome.agents == {SOLO: "done"}
    assert {r.agent for r in llm.requests} == {SOLO}
    first = llm.requests[0]
    assert {t["name"] for t in first.tools} == {t for tools in INVESTIGATOR_TOOLS.values() for t in tools}
    assert first.prompt_version.startswith("solo@")
    # The queries, the answer, and one rewrite (the made-up id).
    assert store.run_rows[(INCIDENT, SOLO)].steps == 3
    assert [e.kind for e in store.board[INCIDENT]] == ["diagnosis"]


def test_the_single_agent_gets_the_three_investigators_steps_together() -> None:
    def never_done(req: LLMRequest, n: int) -> dict[str, Any]:
        return solo_script(req, 99) if req.force_final else {"actions": [{"tool": "prom_query", "args": ERROR_RATIO}]}

    llm = ScriptedLLM({SOLO: never_done})
    asyncio.run(investigate(incident(), deps(llm, MemoryAgentStore()), config("single", max_steps=4)))
    assert len([r for r in llm.requests if not r.force_final]) == 3 * 4 - 1 and llm.requests[11].force_final


def test_the_strong_plan_variant_moves_only_the_coordinator_and_synthesizer() -> None:
    llm = ScriptedLLM(payment_scripts())
    asyncio.run(investigate(incident(), deps(llm, MemoryAgentStore()), config("strong-plan")))
    models = {r.agent: r.model for r in llm.requests}
    assert models == {
        "coordinator": variants.STRONG_MODEL,
        "investigator.metrics": "claude-haiku-5-5",
        "investigator.logs_traces": "claude-haiku-5-5",
        "investigator.changes": "claude-haiku-5-5",
        "synthesizer": variants.STRONG_MODEL,
    }


def test_the_canary_changes_only_the_synthesizer_prompt() -> None:
    default, canary = ScriptedLLM(payment_scripts()), ScriptedLLM(payment_scripts())
    asyncio.run(investigate(incident(), deps(default, MemoryAgentStore()), config("default")))
    asyncio.run(investigate(incident(), deps(canary, MemoryAgentStore()), config("canary")))
    versions = {r.agent: r.prompt_version for r in default.requests}
    changed = {r.agent for r in canary.requests if versions[r.agent] != r.prompt_version}
    assert changed == {"synthesizer"}
    system = next(r.system for r in canary.requests if r.agent == "synthesizer")
    assert "Rank the service the alert fired on first" in system
    assert system != prompts.synthesizer().text


def test_tenants_map_to_variants_and_unknown_names_are_refused() -> None:
    mapping = {"lab-single": "single"}
    assert variants.for_tenant("lab-single", mapping).mode == "single"
    assert variants.for_tenant("lab", mapping) is variants.DEFAULT
    with pytest.raises(ValueError, match="unknown agent variant"):
        variants.for_tenant("x", {"x": "nope"})
