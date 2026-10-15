"""Contract tests: the tools against what the real lab answered.

``tests/fixtures/lab`` holds the cassettes one lab run recorded while each fault was active
(``python -m scripts.lab fixtures``), and what every probe saw live. Replaying them here,
without a cluster, must reproduce the live results exactly: the same check details and the
same evidence IDs. An evidence ID hashes the raw result, so equal IDs mean equal data.

When the probes or a summary change on purpose, these fail until a new lab run is imported.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.tools import ALL_TOOLS, ToolContext, ToolRegistry
from app.tools.base import canonical_json, sha256_hex
from app.tools.evidence import MemoryEvidenceStore
from app.tools.sources import Cassette, ReplaySource
from scripts.lab.probes import PROBES, run_probes

FIXTURES = Path(__file__).parent / "fixtures" / "lab"
MANIFEST = json.loads((FIXTURES / "expected.json").read_text())
FAULTS = sorted(MANIFEST["faults"])


def _registry(fault: str) -> tuple[ToolRegistry, MemoryEvidenceStore, Cassette]:
    cassette = Cassette.load(FIXTURES / f"{fault}.jsonl.gz")
    store = MemoryEvidenceStore()
    return ToolRegistry(ALL_TOOLS, ReplaySource(cassette), store), store, cassette


def test_the_fixtures_come_from_a_passing_run_of_every_acceptance_fault() -> None:
    assert set(FAULTS) == {"payment-failure", "network-delay", "bad-release"}
    for fault, probes in MANIFEST["faults"].items():
        assert [p["name"] for p in probes] == [p.name for p in PROBES[fault]], fault
        assert all(p["ok"] for p in probes), (fault, [p["detail"] for p in probes if not p["ok"]])


@pytest.mark.parametrize("fault", FAULTS)
def test_replay_reproduces_what_the_probes_saw_live(fault: str) -> None:
    registry, store, _ = _registry(fault)
    results = asyncio.run(run_probes(registry, PROBES[fault], "lab", fault))
    got = [
        {"name": r.name, "tool": r.tool, "ok": r.ok, "detail": r.detail, "evidence_id": r.evidence_id} for r in results
    ]
    assert got == MANIFEST["faults"][fault]
    assert {e.source for e in store.evidence.values()} == {"replay"}


@pytest.mark.parametrize("fault", FAULTS)
def test_every_recorded_answer_summarizes_within_bounds(fault: str) -> None:
    registry, store, cassette = _registry(fault)
    ctx = ToolContext(tenant_id="lab", scope=f"contract/{fault}")
    raw_bytes = model_bytes = 0
    for entry in cassette.entries:
        outcome = asyncio.run(registry.call(entry["tool"], entry["args"], ctx))
        assert outcome.error is None, (entry["tool"], outcome.error)
        shown = canonical_json(outcome.for_model())
        assert len(shown) <= 6500, (entry["tool"], len(shown))  # bound() caps the summary at 6000
        evidence = store.evidence[outcome.evidence_id or ""]
        assert evidence.result_hash == sha256_hex(canonical_json(entry["raw"]))
        raw_bytes += len(canonical_json(entry["raw"]))
        model_bytes += len(shown)
    # What the model reads is a small fraction of what the backends returned.
    assert model_bytes * 10 < raw_bytes, (model_bytes, raw_bytes)


def test_trace_get_flags_the_incomplete_trace_from_the_network_delay() -> None:
    # The lab's slowest trace: frontend-proxy gave up after 15 s, and the frontend server span
    # under it is not in the trace, so the proxy looks like the slowest work. The summary says so.
    registry, _, cassette = _registry("network-delay")
    entry = next(e for e in cassette.entries if e["tool"] == "trace_get")
    outcome = asyncio.run(registry.call("trace_get", entry["args"], ToolContext(tenant_id="lab")))
    summary = outcome.summary or {}
    assert summary["orphan_spans"] > 0
    assert any(r["service"] == "product-catalog" for r in summary["slowest_spans"])


def _k8s_events(fault: str) -> list[dict[str, Any]]:
    _, _, cassette = _registry(fault)
    return [e for e in cassette.entries if e["tool"] == "k8s_events"]


@pytest.mark.parametrize("fault", [f for f in FAULTS if _k8s_events(f)])
def test_chaos_mesh_never_reaches_the_evidence(fault: str) -> None:
    # Filtered at fetch time, so not even the recording has it.
    for entry in _k8s_events(fault):
        assert "chaos-mesh.org" not in canonical_json(entry["raw"])
        assert "Chaos" not in canonical_json(entry["raw"])
