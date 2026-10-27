"""M2 acceptance: inject a fault, see the alert reach sre-blackboard, recover, see it resolve.

First the baseline: with no fault injected, no lab alert may fire or go pending for
``baseline_s``. Alerts on a healthy lab are noise, and every later result would be
suspect (lab runs 5 and 6 passed every fault while frontend sat above the slow burn
threshold).

Then, for each fault:

1. Wait until the lab is quiet: no lab alert firing *or pending* for ``hold_s`` in a row.
   One quiet instant is not enough; a flapping alert from the previous fault would
   otherwise be counted against the next one.
2. Inject. Within ``alert_timeout`` (5 minutes) a firing alert must be stored by the API,
   i.e. Alertmanager's webhook got through authentication and ingest, and at least one
   alert must be on a service the catalogue expects (``Fault.symptoms``).
3. Keep collecting alerts for ``settle_s`` to record the full set of symptoms.
4. Recover. Within ``resolve_timeout`` every alert that fired must have a resolved
   notification, and the lab must be quiet again (as in step 1).

An alert that resolves while its fault is still active, or fires again after resolving,
is flapping: it would page twice or close an incident early, so it fails the fault.

With a probe (M3, scripts/lab/probes.py), step 3 also calls the read-only tools while the
fault is active; every tool check must pass.

Timestamps come from the database clock, so host and container clocks need not agree.

For the evaluation (M7) a fault can come with distractors: other faults injected first, each
followed by a wait (a harmless rollout shortly before, an unrelated fault alongside). Alerts
and incidents count from the first injection; times are still measured from the fault's own.
They are recovered after it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

from sqlalchemy import Engine, text

from scripts.lab.cluster import Lab
from scripts.lab.diagnose import Diagnoser, DiagnosisSeen, primary
from scripts.lab.faults import Fault
from scripts.lab.probes import ProbeResult
from scripts.lab.remediate import RemediationSeen, Remediator

Log = Callable[[str], None]
Probe = Callable[[Fault], list[ProbeResult]]


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
    flapping: list[str] = field(default_factory=list)
    probes: list[ProbeResult] = field(default_factory=list)
    # M4: the agents' diagnoses of the incidents this fault opened (when the run asks for them).
    diagnosed: bool = False
    diagnoses: list[DiagnosisSeen] = field(default_factory=list)
    # M7: the same incidents in the other variants' tenants.
    variant_diagnoses: list[DiagnosisSeen] = field(default_factory=list)
    distractors: list[str] = field(default_factory=list)
    cause_at: datetime | None = None  # database time the fault itself was injected
    # M5: the plans for those incidents, what the acceptance run decided, and how it ended.
    repair: str | None = None  # the fault's known repair, "rollout_undo checkout"
    remediated: bool = False
    remediations: list[RemediationSeen] = field(default_factory=list)
    # M7: the worst each SLI got while waiting for the alert, per service, to tell a fault the
    # alert rules cannot see from one that needed longer: {"ad": {"errors": 0.096, ...}}.
    peaks: dict[str, dict[str, float]] = field(default_factory=dict)
    error: str | None = None

    @property
    def answer(self) -> DiagnosisSeen | None:
        return primary(self.diagnoses)

    @property
    def diagnosis_correct(self) -> bool:
        return self.answer is not None and self.answer.top == self.root_cause

    @property
    def closed_loop(self) -> bool:
        return any(r.closed_loop for r in self.remediations)

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
            and self.symptom_matched
            and not self.flapping
            and all(p.ok for p in self.probes)
            and (not self.diagnosed or (self.answer is not None and self.answer.top is not None))
            and (not self.remediated or self.repair is None or self.closed_loop)
            and self.resolved_s is not None
        )


# The SLIs the alert rules use (deploy/kind/rules/slo.yml), and where they page.
SLIS = {
    "errors": "service:sli_error_ratio:rate1m",
    "p95_ms": "service:sli_latency_p95_ms:rate5m",
    "rps": "service:sli_requests:rate5m",
}
PAGES_AT = {"errors": "14.4% fast, 6% slow", "p95_ms": "1000ms"}
SLI_EVERY_S = 30


def _sample_slis(lab: Lab, peaks: dict[str, dict[str, float]]) -> None:
    """Fold the current SLI values into ``peaks``. Best effort: a missed sample only makes the
    peaks less exact."""
    for name, series in SLIS.items():
        try:
            rows = lab.query(series)
        except Exception:  # diagnostics only
            return
        for row in rows:
            service = row.get("metric", {}).get("service_name")
            try:
                value = float(row["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if service and value == value:  # not NaN
                slot = peaks.setdefault(service, {})
                slot[name] = max(slot.get(name, 0.0), value)


def describe_peaks(peaks: dict[str, dict[str, float]], services: Sequence[str]) -> str:
    parts = []
    for service in dict.fromkeys(services):
        p = peaks.get(service)
        if p is None:
            parts.append(f"{service}: no data")
            continue
        parts.append(
            f"{service}: errors {p.get('errors', 0):.1%}, p95 {p.get('p95_ms', 0):.0f}ms, {p.get('rps', 0):.2f} req/s"
        )
    return "; ".join(parts) + f" (pages at errors {PAGES_AT['errors']}, p95 {PAGES_AT['p95_ms']})"


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


def _key(row: Any) -> str:
    """One Alertmanager alert. The stored fingerprint is the incident's (alert name, service,
    environment), which fast and slow burn share, so the label set tells them apart."""
    return json.dumps(dict(row.labels), sort_keys=True)


def _label(labels: dict[str, str]) -> str:
    burn = f"[{labels['burn']}]" if "burn" in labels else ""
    return f"{labels.get('alertname', '?')}{burn}"


def _lab_alerts(lab: Lab) -> list[dict[str, Any]]:
    """Firing and pending lab alerts in Prometheus."""
    return [a for a in lab.alerts() if a["labels"].get("environment") == "lab"]


def _name(alert: dict[str, Any]) -> str:
    return f"{alert['labels'].get('alertname')}/{alert['labels'].get('service_name')} ({alert['state']})"


def _names(alerts: Sequence[dict[str, Any]]) -> str:
    return ", ".join(sorted({_name(a) for a in alerts}))


def wait_quiet(lab: Lab, timeout_s: float, log: Log, hold_s: float = 60, poll_s: float = 10) -> bool:
    """Wait until no lab alert is firing or pending for ``hold_s`` in a row."""
    deadline = time.monotonic() + timeout_s
    quiet_since: float | None = None
    while True:
        active = _lab_alerts(lab)
        now = time.monotonic()
        if active:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = now
        if quiet_since is not None and now - quiet_since >= hold_s:
            return True
        if now > deadline:
            log(f"not quiet after {timeout_s:.0f}s: {_names(active) or 'quiet, but not for long enough'}")
            return False
        time.sleep(poll_s)


def check_baseline(lab: Lab, duration_s: float, log: Log, poll_s: float = 10) -> list[str]:
    """Watch the lab with no fault injected; return every alert that fired or went pending."""
    deadline = time.monotonic() + duration_s
    seen: set[str] = set()
    while True:
        for name in sorted({_name(a) for a in _lab_alerts(lab)}):
            if name not in seen:
                seen.add(name)
                log(f"[baseline] {name}")
        if time.monotonic() >= deadline:
            return sorted(seen)
        time.sleep(poll_s)


def _flapping(rows: Sequence[Any], seen: dict[str, AlertSeen], t1: datetime) -> list[str]:
    """Alerts that resolved before recovery started, or fired again after resolving."""
    out = set()
    history: dict[str, list[Any]] = {}
    for r in rows:
        history.setdefault(_key(r), []).append(r)
    for key, alert in seen.items():
        statuses = [r.status for r in history.get(key, [])]
        early = any(r.status == "resolved" and r.received_at < t1 for r in history.get(key, []))
        refired = any(a == "resolved" and b == "firing" for a, b in pairwise(statuses))
        if early or refired:
            out.add(f"{alert.alertname}/{alert.service}")
    return sorted(out)


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
    hold_s: float = 60,
    poll_s: float = 5,
    probe: Probe | None = None,
    diagnoser: Diagnoser | None = None,
    remediator: Remediator | None = None,
    distractors: Sequence[tuple[Fault, float]] = (),
    label: str | None = None,
) -> ScenarioResult:
    """``distractors``: (fault, seconds to wait after injecting it), injected before ``fault``.
    ``label``: what the log lines and recordings are called (default: the fault's name)."""
    name = label or fault.name
    result = ScenarioResult(fault.name, fault.root_cause, list(fault.symptoms))
    result.distractors = [d.name for d, _ in distractors]
    result.repair = " ".join(fault.repair) if fault.repair else None
    repaired_at: datetime | None = None  # database time of the approval
    result.quiet_before = wait_quiet(lab, quiet_timeout, log, hold_s=hold_s, poll_s=poll_s)
    if not result.quiet_before:
        result.error = "lab was not quiet before injection"
        return result

    first = t0 = _db_now(engine)  # alerts and incidents count from the first injection
    injected: list[Fault] = []
    seen: dict[str, AlertSeen] = {}
    try:
        for distractor, wait_s in distractors:
            log(f"[{name}] distractor: {distractor.description}")
            lab.inject(distractor)
            injected.append(distractor)
            time.sleep(wait_s)
        t0 = result.cause_at = _db_now(engine)
        log(f"[{name}] inject: {fault.description}")
        injected.append(fault)
        lab.inject(fault)
        deadline = time.monotonic() + alert_timeout
        settle_until: float | None = None
        next_sample = time.monotonic()
        while True:
            if settle_until is None and time.monotonic() >= next_sample:
                _sample_slis(lab, result.peaks)
                next_sample = time.monotonic() + SLI_EVERY_S
            for row in _alert_rows(engine, tenant, first):
                key = _key(row)
                if row.status == "firing" and key not in seen:
                    labels = dict(row.labels)
                    seen[key] = AlertSeen(
                        alertname=_label(labels),
                        service=labels.get("service_name") or labels.get("service") or "?",
                        labels=labels,
                        seconds_after_injection=round((row.received_at - t0).total_seconds(), 1),
                    )
                    result.incident_created = result.incident_created or row.incident_id is not None
                    log(
                        f"[{name}] alert {seen[key].alertname} on {seen[key].service} "
                        f"after {seen[key].seconds_after_injection}s"
                    )
            if seen and settle_until is None:
                result.first_alert_s = min(a.seconds_after_injection for a in seen.values())
                settle_until = time.monotonic() + settle_s
            now = time.monotonic()
            if settle_until is not None and now > settle_until:
                break
            if settle_until is None and now > deadline:
                log(f"[{name}] no alert within {alert_timeout:.0f}s")
                watched = [fault.root_cause, *fault.symptoms]
                log(f"[{name}] peak SLIs: {describe_peaks(result.peaks, watched)}")
                break
            time.sleep(poll_s)
        if probe is not None and seen:
            result.probes = probe(fault)
            for p in result.probes:
                log(f"[{name}] tool {p.tool} ({p.name}): {'ok' if p.ok else 'FAIL'} - {p.detail}")
        if diagnoser is not None and seen:
            result.diagnosed = True
            meta = {"scenario": name, "cause_at": t0.isoformat()}
            everything = diagnoser.wait(fault, first, log, label=name, meta=meta)
            result.diagnoses = [d for d in everything if d.tenant == tenant]
            result.variant_diagnoses = [d for d in everything if d.tenant != tenant]
            if result.answer is not None:
                verdict = "correct" if result.diagnosis_correct else f"wrong (expected {fault.root_cause})"
                log(f"[{name}] answer: {result.answer.top or 'no root cause'} - {verdict}")
        if remediator is not None and result.diagnoses:
            result.remediated = True
            decided_at = _db_now(engine)
            result.remediations = remediator.run(fault, result.diagnoses, log, label=name)
            if any(r.decision == "approve" for r in result.remediations):
                repaired_at = decided_at
    except Exception as exc:  # report and still try to recover; one broken fault should not stop the run
        result.error = f"{type(exc).__name__}: {exc}"
        log(f"[{name}] error: {result.error}")
    finally:
        result.alerts = sorted(seen.values(), key=lambda a: a.seconds_after_injection)
        # A fault sre-blackboard repaired counts from the approval: its alerts may have resolved
        # before the lab's own recovery step, which then finds nothing to undo.
        t1 = repaired_at or _db_now(engine)
        log(f"[{name}] recover")
        for f in reversed(injected):
            if not lab.recover(f) and f is fault and repaired_at is not None:
                log(f"[{name}] nothing to recover: sre-blackboard already repaired it")

    deadline_r = time.monotonic() + resolve_timeout
    quiet_since: datetime | None = None  # database time; resolution counts from here
    while True:
        rows = _alert_rows(engine, tenant, first)
        latest = {_key(r): r.status for r in rows}
        open_keys = {k for k in seen if latest.get(k) != "resolved"}
        active = _lab_alerts(lab)
        db_now = _db_now(engine)
        if open_keys or active:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = db_now
        if quiet_since is not None and (db_now - quiet_since).total_seconds() >= hold_s:
            result.resolved_s = round((quiet_since - t1).total_seconds(), 1)
            log(f"[{name}] all alerts resolved {result.resolved_s}s after recovery")
            break
        if time.monotonic() > deadline_r:
            result.unresolved = sorted(f"{seen[k].alertname}/{seen[k].service}" for k in open_keys)
            log(f"[{name}] not resolved after {resolve_timeout:.0f}s: {result.unresolved or _names(active)}")
            break
        time.sleep(poll_s)
    if diagnoser is not None:
        log(f"[{name}] closed {diagnoser.close(first)} incident(s)")
    result.flapping = _flapping(_alert_rows(engine, tenant, first), seen, t1)
    if result.flapping:
        log(f"[{name}] flapping: {', '.join(result.flapping)}")
    if not result.symptom_matched and seen:
        log(f"[{name}] no alert on an expected service ({', '.join(fault.symptoms)})")
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
        [
            {
                **asdict(r),
                "ok": r.ok(alert_timeout),
                "symptom_matched": r.symptom_matched,
                "diagnosis_correct": r.diagnosis_correct if r.diagnosed else None,
                "closed_loop": r.closed_loop if r.remediated and r.repair else None,
            }
            for r in results
        ],
        indent=2,
        default=str,
    )


def to_markdown(results: Sequence[ScenarioResult], alert_timeout: float) -> str:
    lines = [
        "| fault | root cause | first alert | alerts (service) | expected symptom seen | flapping | tool checks "
        "| resolved after recovery | result |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        alerts = ", ".join(f"{a.alertname} ({a.service}, {a.seconds_after_injection:.0f}s)" for a in r.alerts) or "-"
        first = f"{r.first_alert_s:.0f}s" if r.first_alert_s is not None else "none"
        resolved = f"{r.resolved_s:.0f}s" if r.resolved_s is not None else f"no ({', '.join(r.unresolved)})"
        flapping = ", ".join(r.flapping) or "-"
        failed = [p.name for p in r.probes if not p.ok]
        tools = "-" if not r.probes else f"{len(r.probes) - len(failed)}/{len(r.probes)}"
        if failed:
            tools += f" (failed: {', '.join(failed)})"
        verdict = "ok" if r.ok(alert_timeout) else f"FAIL{': ' + r.error if r.error else ''}"
        lines.append(
            f"| {r.fault} | {r.root_cause} | {first} | {alerts} | {'yes' if r.symptom_matched else 'no'} "
            f"| {flapping} | {tools} | {resolved} | {verdict} |"
        )
    diagnosed = [r for r in results if r.diagnosed]
    if diagnosed:
        lines += [
            "",
            "| fault | root cause | first incident | top candidate | correct | all incidents (top) | time | calls | tokens | cost |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in diagnosed:
            a = r.answer
            if a is None:
                lines.append(f"| {r.fault} | {r.root_cause} | none | - | no | - | - | - | - | - |")
                continue
            top = ", ".join(f"{c['service']} {c['confidence']:.0%}" for c in a.root_causes[:2]) or f"none ({a.reason})"
            others = ", ".join(f"{d.service}: {d.top or d.state.lower()}" for d in r.diagnoses)
            took = f"{a.investigation_s:.0f}s" if a.investigation_s is not None else "-"
            cost = f"${sum(d.cost_usd or 0 for d in r.diagnoses):.3f}"
            lines.append(
                f"| {r.fault} | {r.root_cause} | {a.title}/{a.service} | {top} | {'yes' if r.diagnosis_correct else 'no'} "
                f"| {others} | {took} | {sum(d.calls for d in r.diagnoses)} | {sum(d.tokens for d in r.diagnoses):,} | {cost} |"
            )
        correct = sum(r.diagnosis_correct for r in diagnosed)
        lines += ["", f"Top-1 correct: {correct}/{len(diagnosed)} (needed: {required_correct(len(diagnosed))})"]
    remediated = [r for r in results if r.remediated]
    if remediated:
        lines += [
            "",
            "| fault | incident | plan | policy | decision | outcome | time | executions |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in remediated:
            for m in r.remediations:
                policy = f"{m.verdict} ({m.risk})" if m.risk else (m.verdict or "-")
                took = f"{m.approved_to_final_s:.0f}s" if m.approved_to_final_s is not None else "-"
                lines.append(
                    f"| {r.fault} | {m.service} | {', '.join(m.plan) or 'no action'} | {policy} | {m.decision or '-'} "
                    f"| {m.state.lower()}{': ' + m.reason if m.decision else ''} | {took} | {'; '.join(m.executions) or '-'} |"
                )
        for r in remediated:
            if r.repair is not None:
                loop = "closed" if r.closed_loop else "NOT closed"
                lines += ["", f"Closed loop for {r.fault} ({r.repair}): {loop}"]
    return "\n".join(lines) + "\n"


def required_correct(n: int) -> int:
    """M4 acceptance: the top candidate is right for at least 2 of the 3 faults."""
    return -(-2 * n // 3)
