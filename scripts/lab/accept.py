"""M2 acceptance: inject a fault, see the alert reach sre-blackboard, recover, see it resolve.

For each fault:

1. Wait until Prometheus has no firing lab alerts (a false positive here fails the run).
2. Inject. Within ``alert_timeout`` (5 minutes) a firing alert must be stored by the API,
   i.e. Alertmanager's webhook got through authentication and ingest.
3. Keep collecting alerts for ``settle_s`` to record the full set of symptoms.
4. Recover. Within ``resolve_timeout`` every alert that fired must have a resolved
   notification, and Prometheus must be quiet again.

Timestamps come from the database clock, so host and container clocks need not agree.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text

from scripts.lab.cluster import Lab
from scripts.lab.faults import Fault

Log = Callable[[str], None]


@dataclass
class AlertSeen:
    alertname: str
    service: str
    labels: dict[str, str]
    seconds_after_injection: float


@dataclass
class ScenarioResult:
    fault: str
    root_cause: str
    expected_symptoms: list[str]
    quiet_before: bool = False
    first_alert_s: float | None = None
    alerts: list[AlertSeen] = field(default_factory=list)
    incident_created: bool = False
    resolved_s: float | None = None
    unresolved: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def symptom_matched(self) -> bool:
        return any(a.service in self.expected_symptoms for a in self.alerts)

    def ok(self, alert_timeout: float) -> bool:
        return (
            self.error is None
            and self.quiet_before
            and self.first_alert_s is not None
            and self.first_alert_s <= alert_timeout
            and self.incident_created
            and self.resolved_s is not None
        )


def _db_now(engine: Engine) -> datetime:
    with engine.connect() as conn:
        now: datetime = conn.execute(text("SELECT now()")).scalar_one()
        return now


def _alert_rows(engine: Engine, tenant: str, since: datetime) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT fingerprint, status, labels, incident_id, received_at FROM alerts "
                    "WHERE tenant_id = :t AND received_at >= :since ORDER BY id"
                ),
                {"t": tenant, "since": since},
            )
        )


def _lab_alerts(lab: Lab) -> list[dict[str, Any]]:
    return [a for a in lab.firing_alerts() if a["labels"].get("environment") == "lab"]


def wait_quiet(lab: Lab, timeout_s: float, log: Log, poll_s: float = 10) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        firing = _lab_alerts(lab)
        if not firing:
            return True
        if time.monotonic() > deadline:
            names = sorted({f"{a['labels'].get('alertname')}/{a['labels'].get('service_name')}" for a in firing})
            log(f"still firing after {timeout_s:.0f}s: {', '.join(names)}")
            return False
        time.sleep(poll_s)


def run_scenario(
    lab: Lab,
    engine: Engine,
    fault: Fault,
    *,
    tenant: str,
    log: Log,
    alert_timeout: float = 300,
    settle_s: float = 60,
    resolve_timeout: float = 600,
    quiet_timeout: float = 600,
    poll_s: float = 5,
) -> ScenarioResult:
    result = ScenarioResult(fault.name, fault.root_cause, list(fault.symptoms))
    result.quiet_before = wait_quiet(lab, quiet_timeout, log)
    if not result.quiet_before:
        result.error = "lab was not quiet before injection"
        return result

    t0 = _db_now(engine)
    log(f"[{fault.name}] inject: {fault.description}")
    seen: dict[str, AlertSeen] = {}
    try:
        lab.inject(fault)
        deadline = time.monotonic() + alert_timeout
        settle_until: float | None = None
        while True:
            for row in _alert_rows(engine, tenant, t0):
                if row.status == "firing" and row.fingerprint not in seen:
                    labels = dict(row.labels)
                    seen[row.fingerprint] = AlertSeen(
                        alertname=labels.get("alertname", "?"),
                        service=labels.get("service_name") or labels.get("service") or "?",
                        labels=labels,
                        seconds_after_injection=round((row.received_at - t0).total_seconds(), 1),
                    )
                    result.incident_created = result.incident_created or row.incident_id is not None
                    log(
                        f"[{fault.name}] alert {seen[row.fingerprint].alertname} on {seen[row.fingerprint].service} "
                        f"after {seen[row.fingerprint].seconds_after_injection}s"
                    )
            if seen and settle_until is None:
                result.first_alert_s = min(a.seconds_after_injection for a in seen.values())
                settle_until = time.monotonic() + settle_s
            now = time.monotonic()
            if settle_until is not None and now > settle_until:
                break
            if settle_until is None and now > deadline:
                log(f"[{fault.name}] no alert within {alert_timeout:.0f}s")
                break
            time.sleep(poll_s)
    except Exception as exc:  # report and still try to recover; one broken fault should not stop the run
        result.error = f"{type(exc).__name__}: {exc}"
        log(f"[{fault.name}] error: {result.error}")
    finally:
        result.alerts = sorted(seen.values(), key=lambda a: a.seconds_after_injection)
        t1 = _db_now(engine)
        log(f"[{fault.name}] recover")
        lab.recover(fault)

    deadline_r = time.monotonic() + resolve_timeout
    while True:
        # An alert may flap (fire, resolve, fire again): what counts is its latest notification.
        latest = {r.fingerprint: r.status for r in _alert_rows(engine, tenant, t0)}
        open_fps = {fp for fp in seen if latest.get(fp) != "resolved"}
        if not open_fps and not _lab_alerts(lab):
            result.resolved_s = round((_db_now(engine) - t1).total_seconds(), 1)
            log(f"[{fault.name}] all alerts resolved {result.resolved_s}s after recovery")
            break
        if time.monotonic() > deadline_r:
            result.unresolved = sorted(f"{seen[fp].alertname}/{seen[fp].service}" for fp in open_fps)
            log(
                f"[{fault.name}] unresolved after {resolve_timeout:.0f}s: {result.unresolved or 'prometheus still firing'}"
            )
            break
        time.sleep(poll_s)
    return result


def run(
    lab: Lab,
    engine: Engine,
    faults: Sequence[Fault],
    *,
    tenant: str,
    log: Log = print,
    alert_timeout: float = 300,
    **kwargs: Any,
) -> list[ScenarioResult]:
    return [run_scenario(lab, engine, f, tenant=tenant, log=log, alert_timeout=alert_timeout, **kwargs) for f in faults]


def to_json(results: Sequence[ScenarioResult], alert_timeout: float) -> str:
    return json.dumps(
        [{**asdict(r), "ok": r.ok(alert_timeout), "symptom_matched": r.symptom_matched} for r in results],
        indent=2,
        default=str,
    )


def to_markdown(results: Sequence[ScenarioResult], alert_timeout: float) -> str:
    lines = [
        "| fault | root cause | first alert | alerts (service) | expected symptom seen | resolved after recovery | result |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        alerts = ", ".join(f"{a.alertname} ({a.service}, {a.seconds_after_injection:.0f}s)" for a in r.alerts) or "-"
        first = f"{r.first_alert_s:.0f}s" if r.first_alert_s is not None else "none"
        resolved = f"{r.resolved_s:.0f}s" if r.resolved_s is not None else f"no ({', '.join(r.unresolved)})"
        verdict = "ok" if r.ok(alert_timeout) else f"FAIL{': ' + r.error if r.error else ''}"
        lines.append(
            f"| {r.fault} | {r.root_cause} | {first} | {alerts} | {'yes' if r.symptom_matched else 'no'} | {resolved} | {verdict} |"
        )
    return "\n".join(lines) + "\n"
