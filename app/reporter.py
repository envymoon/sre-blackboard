"""Reporter: final diagnosis report. Maps to spec Sec.18 (10 fixed sections),
Sec.27.4 sample output, and Sec.25.6 approval binding.

The 10 fixed sections:
1 summary / 2 priority / 3 timeline / 4 Problem vs Event hosts /
5 ranked root-cause candidates / 6 evidence references /
7 completed actions / 8 recommended action + risk + approval + rollback /
9 open items and next steps / 10 run ID, trace link, versions.

Must distinguish: confirmed facts / high-confidence inference /
pending hypotheses / undetermined. The root cause is worded as a
"HIGH_CONFIDENCE_CANDIDATE", never an absolute claim (Sec.27.4).
"""
from __future__ import annotations

CONFIG_VERSION = "2.0.0"

EVIDENCE_TABLE = [
    {"id": "METRIC-1001", "source": "metrics query", "result": "Order success rate dropped from 98.6% to 71.2%; inventory-deduction timeout ratio rising", "role": "Confirms real user-facing impact"},
    {"id": "LOG-7782", "source": "Elasticsearch logs", "result": "TimeoutException concentrated in inventory-deduction calls", "role": "Confirms error type and onset time"},
    {"id": "TRACE-3304", "source": "trace query", "result": "gateway -> checkout-service -> stock-db; failures concentrated in inventory deduction", "role": "Confirms failure propagation path"},
    {"id": "CHANGE-2201", "source": "release record", "result": "checkout-api 2026.09.15.3 released 8 minutes before onset", "role": "Establishes temporal correlation"},
    {"id": "CODE-4402", "source": "code analysis", "result": "2026.09.15.3 added retries on inventory-deduction failures with fixed backoff and no proper total-attempt cap", "role": "Identifies the change that may amplify DB pressure"},
    {"id": "KB-0901", "source": "knowledge retrieval", "result": "Similar incidents: check retry amplification, DB pool saturation, inventory-deduction timeouts first", "role": "Provides triage playbook; never confirms root cause alone"},
]


def build_report(alert: dict, approval: dict | None = None, execution: dict | None = None) -> dict:
    approval = approval or {
        "risk_level": "R3", "action": "Roll back checkout-api to 2026.09.14.9",
        "approver": "duty officer", "decision": "approved",
        "scope": "This incident and action_plan_id=AP-20260915-01",
        "expires_at": "2026-09-15 10:30:00",
    }
    execution = execution or {
        "tool": "rollback_release", "execution_id": "ROLLBACK-20260915-001",
        "service": "checkout-service", "workload": "Deployment/checkout-api",
        "from_version": "2026.09.15.3", "to_version": "2026.09.14.9",
        "status": "completed", "rollback_time": "2026-09-15 10:18:40",
    }
    return {
        # 1 summary
        "summary": "Since 2026-09-15 10:02, prod checkout-service (Deployment/checkout-api) raised a P0: order success rate 98.6% -> 71.2%, SLO error budget burning fast, dominant error is inventory-deduction timeout.",
        # 2 priority
        "priority": {"value": "P0", "recommendation": "Hold P0", "reason": "Core order path failing at scale, burn 12%/5m"},
        # 3 timeline
        "timeline": [
            "09:56:40 released checkout-api 2026.09.15.3 (chg-20260915-884)",
            "10:02:00 alert ALERT-20260915-0001 fired",
            "10:0x Metrics/Log/Trace agents collected evidence in parallel, artifacts published",
            "10:1x Coordinator proposed rollback, Safety rated R3, human approved",
            "10:18:40 rollback_release completed",
            "10:18-10:28 recovery verification window (10 minutes)",
        ],
        # 4 host attribution: Problem hosts are candidates only, Event hosts are observation points only
        "hosts": {
            "problem_hosts": ["checkout-pod-7f8c"],
            "problem_note": "K8s Pod, root-cause candidate only",
            "event_hosts": ["prod-gw-03"],
            "event_note": "Observation point only, not a root cause",
        },
        # 5 root-cause candidates: high-confidence candidate, not absolute
        "root_cause_candidates": [{
            "rank": 1, "confidence": "HIGH_CONFIDENCE_CANDIDATE",
            "statement": "After release 2026.09.15.3 changed retry policy, retries amplified load while stock-db was already queuing on connections; pool exhaustion made inventory deduction time out persistently and propagated to checkout.",
            "supporting": ["METRIC-1001", "LOG-7782", "TRACE-3304", "CHANGE-2201", "CODE-4402"],
        }],
        # 6 evidence
        "evidences": EVIDENCE_TABLE,
        # 7 completed actions
        "completed_actions": ["Collected metrics/log/trace/change/code evidence and published artifacts", "Safety R3 review", "Human approval", "rollback_release to 2026.09.14.9"],
        # 8 recommended action + risk + approval + rollback
        "recommended_action": {
            "action": "Roll back checkout-api to 2026.09.14.9",
            "risk_level": "R3", "approval": approval, "execution": execution,
            "rollback": "Rolled-back version is the rollback state; on anomaly, re-enter approval against the previous stable Deployment revision",
        },
        # 9 open items, next steps, four-state split
        "facts_confirmed": ["Success rate dropped sharply in window", "Errors concentrated in inventory-deduction timeouts", "Failing path goes through inventory service and stock DB", "Onset close to release completion", "New revision contains a load-amplifying change"],
        "hypotheses_pending": ["Whether retry is the sole pool-killer still needs load-test confirmation"],
        "unknown": ["Order dirty writes: no evidence yet, reconciliation required"],
        "next_steps": ["Reconcile orders to rule out dirty writes", "Re-check retry backoff and total-attempt cap", "Promote to KB runbook (after human review)"],
        # 10 run metadata
        "run_meta": {
            "run_id": "RUN-20260915-001", "trace_link": "trace://inc-20260915-0001",
            "report_version": "r1", "config_version": CONFIG_VERSION,
        },
    }


def render_markdown(report: dict) -> str:
    lines = ["# P0 Diagnosis Report (10 fixed sections)", ""]
    lines.append(f"## 1 Summary\n{report['summary']}\n")
    p = report["priority"]
    lines.append(f"## 2 Priority\n{p['value']}, {p['recommendation']}: {p['reason']}\n")
    lines.append("## 3 Timeline\n" + "\n".join(f"- {t}" for t in report["timeline"]) + "\n")
    h = report["hosts"]
    lines.append(f"## 4 Problem/Event Hosts\n- Problem: {h['problem_hosts']} ({h['problem_note']})\n- Event: {h['event_hosts']} ({h['event_note']})\n")
    lines.append("## 5 Root-Cause Candidates (ranked by confidence)")
    for c in report["root_cause_candidates"]:
        lines.append(f"- [{c['confidence']}] {c['statement']} ({','.join(c['supporting'])})")
    lines.append("\n## 6 Evidence")
    for e in report["evidences"]:
        lines.append(f"- {e['id']} {e['source']}: {e['result']} ({e['role']})")
    lines.append("\n## 7 Completed Actions\n" + "\n".join(f"- {a}" for a in report["completed_actions"]) + "\n")
    r = report["recommended_action"]
    lines.append(f"## 8 Recommended Action\n- {r['action']}, risk {r['risk_level']}, approval {r['approval']['decision']} ({r['approval']['approver']}, expires {r['approval']['expires_at']})\n- Execution {r['execution']['execution_id']} {r['execution']['status']} {r['execution']['rollback_time']}\n")
    lines.append("## 9 Open Items and Next Steps")
    lines.append("- Confirmed facts: " + "; ".join(report["facts_confirmed"]))
    lines.append("- Pending hypotheses: " + "; ".join(report["hypotheses_pending"]))
    lines.append("- Undetermined: " + "; ".join(report["unknown"]))
    lines.append("- Next steps: " + "; ".join(report["next_steps"]) + "\n")
    m = report["run_meta"]
    lines.append(f"## 10 Run ID / Trace / Versions\n- {m['run_id']} {m['trace_link']} {m['report_version']} config {m['config_version']}\n")
    return "\n".join(lines)
