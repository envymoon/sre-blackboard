"""The evaluation without a lab or a model: every scenario's recorded run, replayed (CI).

A live evaluation exports each scenario's incidents as a recording (app/agents/recording.py):
the incident rows, every model step and every tool answer. Here they are loaded into an empty
database and run through the worker's own job handlers, one incident at a time: triage,
the investigation, the planner, the policy. The model and the tools answer from the
recording, so the same prompts get the same answers; what runs for real is everything else:
the loop, the citation checks, the state machine, the plan validation and the policy. The
observations are read back exactly as the live run reads them (app/eval/collect.py).

So a change to the deterministic code shows up in the scores (a weaker policy lets an action
on a healthy service through, and the gate fails), and a change to anything the model sees
shows up as misses: the request is not in the recording, and the live evaluation has to be
run again. Misses are counted, not hidden.

Not replayed: the alerts (the incidents are loaded as they were opened), the approval and
what the executor did (nothing is approved here), and wall-clock times (diagnosis latency is
the live run's).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents import recording
from app.core.config import Settings
from app.eval.collect import observe
from app.eval.score import IncidentObs, ScenarioObs
from app.llm.replay import ReplayBackend
from app.queue import jobs
from app.tools.sources import ReplaySource
from app.worker.handlers import triage
from app.worker.investigate import AgentServices, make_investigate
from app.worker.remediate import make_plan
from app.worker.runner import JobContext

REPO = Path(__file__).resolve().parents[2]
RECORDINGS = REPO / "eval" / "recordings"
# The Deployments the lab lets remediation touch (.github/actions/lab-up, SRE_REMEDIATION_TARGETS).
LAB_TARGETS = [
    "checkout",
    "payment",
    "product-catalog",
    "recommendation",
    "frontend",
    "cart",
    "currency",
    "shipping",
    "ad",
    "email",
    "quote",
]
OWNER = "replay"
KINDS = ("triage", "investigate", "plan")


@dataclass
class Replayed:
    observation: ScenarioObs
    llm_misses: int = 0
    tool_misses: int = 0
    notes: list[str] = field(default_factory=list)


def recordings(directory: Path = RECORDINGS) -> list[tuple[str, str, Path]]:
    """(scenario, variant, path) for every recording: <scenario>/ is the default variant,
    <scenario>/<variant>/ the others."""
    out = []
    for path in sorted(p.parent for p in directory.glob("**/incidents.json")):
        rel = path.relative_to(directory).parts
        out.append((rel[0], rel[1] if len(rel) > 1 else "default", path))
    return out


def settings_for(tenant_variants: dict[str, str], agent_max_steps: int) -> Settings:
    return Settings(
        _env_file=None,  # nothing from a local .env or .lab.env
        agent_max_steps=agent_max_steps,
        remediation_targets=LAB_TARGETS,
        eval_tenant_variants=tenant_variants,
        auto_investigate=True,
        auto_remediate=True,
    )


async def _load(engine: AsyncEngine, rec: recording.Recording, investigated: set[str]) -> None:
    """The incidents as they were when opened: the investigated ones RECEIVED, with a triage job."""
    await recording.load_incidents(engine, rec)
    async with engine.begin() as conn:
        for inc in rec.incidents:
            if str(inc["id"]) not in investigated:
                continue
            await conn.execute(
                text("UPDATE incidents SET state = 'RECEIVED', closed_at = NULL WHERE id = :id"), {"id": inc["id"]}
            )
            await jobs.enqueue(
                conn,
                tenant_id=inc["tenant_id"],
                incident_id=inc["id"],
                kind="triage",
                priority=inc["priority"],
                max_attempts=1,
                aging_step_s=0,
                idempotency_key=f"{inc['id']}:triage",
            )


async def _drive(engine: AsyncEngine, settings: Settings, services: AgentServices) -> None:
    """Run every claimable triage, investigate and plan job until none is left."""
    handlers = {"triage": triage, "investigate": make_investigate(services), "plan": make_plan(services)}
    while True:
        async with engine.begin() as conn:
            job = await jobs.claim(conn, owner=OWNER, lease_ttl_s=600, kinds=KINDS)
        if job is None:
            return
        await handlers[job.kind](JobContext(job, engine, settings, OWNER))
        async with engine.begin() as conn:
            await jobs.complete(conn, job)


async def replay(engine: AsyncEngine, scenario: str, variant: str, path: Path) -> Replayed:
    """Replay one recording into ``engine`` (an empty database) and observe the result."""
    rec = recording.Recording.load(path)
    meta: dict[str, Any] = rec.llm.meta
    investigated = sorted(rec.expected)
    tenants = {inc["tenant_id"] for inc in rec.incidents if str(inc["id"]) in rec.expected}
    settings = settings_for({t: variant for t in tenants} if variant != "default" else {}, rec.max_steps)
    await _load(engine, rec, set(investigated))
    backend = ReplayBackend(rec.llm)
    order = sorted(
        (inc for inc in rec.incidents if str(inc["id"]) in rec.expected), key=lambda i: (i["created_at"], str(i["id"]))
    )
    for inc in order:
        # One incident at a time, each answered from its own tool calls: several incidents ask
        # the same question minutes apart and must each get their own answer.
        services = AgentServices(backend, ReplaySource(recording.incident_tools(rec, inc["id"])))
        # Only this incident's jobs are claimable: hold the others back until their turn.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE jobs SET run_after = now() + interval '1 day' WHERE incident_id <> :id AND status = 'queued'"
                ),
                {"id": inc["id"]},
            )
            await conn.execute(
                text("UPDATE jobs SET run_after = now() WHERE incident_id = :id AND status = 'queued'"),
                {"id": inc["id"]},
            )
        await _drive(engine, settings, services)

    # Recorded before M7 without it: the first incident stands in for the injection.
    cause_at = datetime.fromisoformat(str(meta.get("cause_at") or order[0]["created_at"]))
    incidents: list[IncidentObs] = await observe(engine, [UUID(str(i)) for i in investigated], cause_at)
    for i in incidents:
        i.investigation_s = None  # replay time, not the live run's
    out = Replayed(ScenarioObs(scenario=scenario, variant=variant, run="replay", incidents=incidents))
    async with engine.connect() as conn:
        out.llm_misses = await conn.scalar(text("SELECT count(*) FROM llm_calls WHERE error_code = 'replay_miss'")) or 0
        out.tool_misses = (
            await conn.scalar(text("SELECT count(*) FROM tool_calls WHERE error_code = 'replay_miss'")) or 0
        )
    recorded = {k: (v.get("diagnosis") or {}).get("root_causes") for k, v in rec.expected.items()}
    for i in incidents:
        was = [c["service"] for c in recorded.get(i.incident_id) or []]
        if was != i.root_causes:
            out.notes.append(f"{i.service}: recorded {was or 'none'}, replayed {i.root_causes or 'none'}")
    return out


def describe(r: Replayed) -> str:
    misses = f"{r.llm_misses} model / {r.tool_misses} tool misses" if r.llm_misses or r.tool_misses else "no misses"
    tops = ", ".join(f"{i.service}: {i.top or 'none'}" for i in r.observation.incidents)
    return f"{r.observation.scenario} [{r.observation.variant}]: {tops}; {misses}" + "".join(
        f"\n  {n}" for n in r.notes
    )
