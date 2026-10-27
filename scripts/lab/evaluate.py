"""M7: the evaluation on the lab (``python -m scripts.lab eval``).

Each scenario (eval/scenarios) runs like an acceptance fault: wait for a quiet lab, inject
its distractors and then its fault, collect the alerts, wait for every variant's
investigations and plans, approve the labelled repair in the default tenant only (so the
lab is repaired once, by the system under test), recover whatever is left, wait until the
alerts resolve. Then each tenant's incidents are read back as observations
(app/eval/collect.py) and appended to one result file, written after every scenario so a
run cut short keeps what it finished.

The variants run side by side on the same fault: Alertmanager posts every alert to one
tenant per variant (deploy/kind/up.sh, SRE_LAB_VARIANTS), and the workers investigate each
tenant's incidents with its variant (SRE_EVAL_TENANT_VARIANTS). Same alerts, same cluster,
same minute; only the agents differ.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import create_async_engine

from app.eval.collect import observe
from app.eval.results import ResultFile
from app.eval.scenarios import Scenario
from app.eval.score import IncidentObs, ScenarioObs
from scripts.lab import accept, faults
from scripts.lab.cluster import Lab
from scripts.lab.diagnose import Diagnoser
from scripts.lab.faults import Fault
from scripts.lab.remediate import Remediator

Log = Callable[[str], None]


def shard(items: Sequence[Scenario], spec: str | None) -> list[Scenario]:
    """``"2/4"``: the second of four interleaved slices (1-based)."""
    if not spec:
        return list(items)
    i, n = (int(x) for x in spec.split("/"))
    if not 1 <= i <= n:
        raise ValueError(f"shard {spec!r}: expected i/n with 1 <= i <= n")
    return list(items[i - 1 :: n])


def steps(scenario: Scenario) -> tuple[list[tuple[Fault, float]], Fault]:
    """The distractors as (fault, seconds to wait after it), and the fault itself."""
    injections = scenario.inject
    cause_at = next(k for k, i in enumerate(injections) if i.role == "cause")
    if any(i.role == "distractor" for i in injections[cause_at + 1 :]):
        raise ValueError(f"{scenario.id}: distractors must come before the cause")
    before = [(faults.get(i.fault), injections[k + 1].after_s) for k, i in enumerate(injections[:cause_at])]
    return before, faults.get(scenario.cause.fault)


def observations(
    scenario: Scenario,
    result: accept.ScenarioResult,
    variants: dict[str, str],
    incidents: dict[str, list[IncidentObs]],
    run: str,
    alert_timeout: float,
) -> list[ScenarioObs]:
    return [
        ScenarioObs(
            scenario=scenario.id,
            variant=variant,
            run=run,
            first_alert_s=result.first_alert_s,
            alerts=[f"{a.alertname}/{a.service}" for a in result.alerts],
            incidents=incidents.get(tenant, []),
            lab_ok=result.ok(alert_timeout),
            error=result.error,
        )
        for tenant, variant in variants.items()
    ]


async def _observe(database_url: str, ids: list[UUID], cause_at: datetime) -> list[IncidentObs]:
    engine = create_async_engine(database_url)
    try:
        return await observe(engine, ids, cause_at)
    finally:
        await engine.dispose()


def run(
    lab: Lab,
    engine: Engine,
    scenarios: Sequence[Scenario],
    *,
    diagnoser: Diagnoser,
    remediator: Remediator | None,
    database_url: str,
    tenant: str,
    out: Path,
    run_id: str,
    commit: str = "",
    log: Log = print,
    alert_timeout: float = 300,
    **kwargs: Any,
) -> ResultFile:
    results = ResultFile(run=run_id, kind="live", commit=commit)
    for i, scenario in enumerate(scenarios, 1):
        log(f"=== scenario {i}/{len(scenarios)}: {scenario.id} ({scenario.difficulty}): {scenario.title}")
        distractors, fault = steps(scenario)
        timeout = max(alert_timeout, scenario.alert_timeout_s or 0)
        r = accept.run_scenario(
            lab,
            engine,
            fault,
            tenant=tenant,
            log=log,
            alert_timeout=timeout,
            diagnoser=diagnoser,
            remediator=remediator,
            distractors=distractors,
            label=scenario.id,
            **kwargs,
        )
        by_tenant: dict[str, list[IncidentObs]] = {}
        everything = r.diagnoses + r.variant_diagnoses
        if everything and r.cause_at is not None:
            for t in diagnoser.variants:
                ids = [UUID(d.incident_id) for d in everything if d.tenant == t]
                by_tenant[t] = asyncio.run(_observe(database_url, ids, r.cause_at)) if ids else []
        results.observations += observations(scenario, r, diagnoser.variants, by_tenant, run_id, timeout)
        results.dump(out / "results.json")
        log(f"=== {scenario.id}: {'ok' if r.ok(timeout) else 'lab check FAILED'}; {len(everything)} incident(s)")
    return results
