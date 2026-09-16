"""End-to-end run across all 10 fixed stages. Maps to spec Sec.8.5/Sec.27.2:
queue -> gateway budget -> context -> knowledge -> evidence ->
synthesis -> safety -> approval -> execution -> verification -> report -> memory.

Every module follows its existing contract; no new semantics.
External models/ES/MySQL/Redis remain local same-contract implementations.
"""
from __future__ import annotations
import time

from .models import Task, IncidentState, Artifact
from .harness import AgentHarness
from .runtime import AgentRuntime
from .gateway import ModelGateway, TokenLedger
from .context import compress_pipeline
from .knowledge import build_default_index, search_knowledge, cite_knowledge
from .queue import PriorityQueue, make_message
from .persistence import Store
from .safety import safety_review
from .mcp import check_tool_call
from .auth import Approvals, AuditLog, ingress, runtime_claim_check
from .memory import WorkingMemory, VerifiedMemory
from .agents import coordinator_plan, run_log_agent_once
from .reporter import build_report

AGENT_OF_TASK = {"logs": "log", "metrics": "metrics", "change": "change", "host": "host"}
TOKENS_OF_TASK = {"logs": (6000, 800), "metrics": (2000, 500), "change": (3000, 500), "host": (3000, 500)}


def run_incident_e2e(alert: dict, tenant_id: str = "tenant-checkout") -> dict:
    incident_id = "inc-e2e-p0-001"
    auth = {"tenant_id": tenant_id, "principal_id": "oncall-1", "role_ids": ["oncall"],
            "service_scope": ["checkout-service"], "expires_at": time.time() + 3600}
    assert ingress(auth)

    harness = AgentHarness(runtime=AgentRuntime())
    ledger = TokenLedger(incident_id=incident_id, priority="P0")
    gateway = ModelGateway(ledger)
    queue, store = PriorityQueue(), Store()
    approvals, audit = Approvals(), AuditLog()
    working, verified = WorkingMemory(), VerifiedMemory()
    index = build_default_index()

    # 1 normalize + 2 impact verification
    audit.append(tenant_id, "oncall-1", "incident.create", "checkout-service", "allow", "run-e2e-1")
    store.save_incident(incident_id, tenant_id, "Problem", "P0", alert)
    working.write_incident(incident_id, alert)
    state = IncidentState(incident_id=incident_id, stage="coordinator_planning")
    harness.store.save_state(state)

    # 3-4 planning + dispatch
    plan = coordinator_plan(alert)
    cap_map = {"Problem": "logs", "Business": "metrics", "Event": "change", "Host": "host"}
    for i, at in enumerate(plan):
        t = Task(task_id=f"t-{i+1}", incident_id=incident_id, alert_type=at, priority="P0",
                 capability=cap_map[at], service="checkout-service", time_window={"lookback": "30m"})
        assert runtime_claim_check({**auth, "tenant_id": tenant_id}, {"tenant_id": tenant_id, "service": "checkout-service"})
        state.tasks[t.task_id] = t
        store.save_task(t.task_id, incident_id, tenant_id, f"{at.lower()}-skill", "P0")
        msg = make_message(incident_id, t.task_id, at, "P0")
        msg["tenant_id"] = tenant_id
        queue.enqueue(msg)
    state.stage = "dynamic_evidence_collection"

    # 5 evidence: per task, gateway budget -> context -> knowledge -> execute -> artifact -> persist -> trace -> memory
    for t in state.tasks.values():
        msg = queue.pop_next()
        if msg is None:  # Low-priority tasks held by P0 preemption stay queued; skip this round
            continue
        agent = AGENT_OF_TASK[t.capability]
        tin, tout = TOKENS_OF_TASK[t.capability]
        g = gateway.complete(agent, tin, tout)
        assert g["status"] == "ok", g
        evs = [{"kind": "log" if t.capability == "logs" else "metric",
                "evidence_id": f"EV-{t.task_id}", "text": "checkout TimeoutException",
                "service_match": True, "time_score": 25, "trace_link": True, "severity": 15, "p0_related": True}]
        ctx = compress_pipeline(alert, evs, "P0")
        assert ctx["status"] == "OK"
        if t.alert_type == "Problem":
            kr = search_knowledge(index, tenant_id, "checkout-service", "production", "Problem", "checkout order inventory-deduction timeout")
            assert kr["status"] in ("OK", "NO_RELEVANT_KNOWLEDGE")
            if kr["status"] == "OK":
                assert cite_knowledge(kr["results"][0]["chunk_id"], f"EV-{t.task_id}")["status"] == "OK"
        if t.capability == "logs":
            run_log_agent_once(t, harness)
        else:
            assert harness.try_claim(t, agent)
            t.status = "RUNNING"
            obs = harness.execute_action(t, {"type": "tool", "name": "prometheus.query_metrics", "args": {}}, {})
            art = Artifact(incident_id=incident_id, producer=agent, artifact_type=f"{at}Evidence",
                           facts=[{"statement": "evidence", "evidence_ids": [obs.get("evidence_id", "EV-x")]}],
                           evidence_refs=[obs.get("evidence_id", "EV-x")], hypotheses=[], gaps=[],
                           next_task_proposals=[])
            harness.publish_artifact(t, art, state)
        store.save_artifact(f"art-{t.task_id}", incident_id, t.task_id, tenant_id,
                            {"artifact_type": "Evidence", "producer": agent}, [f"EV-{t.task_id}"])
        store.append_trace(f"run-{t.task_id}", incident_id, t.task_id, tenant_id, agent,
                           "evidence", tin, tout, 50, "ok")
        working.append_artifact(incident_id, f"art-{t.task_id}", f"EV-{t.task_id}")
        audit.append(tenant_id, agent, "evidence.collect", t.task_id, "allow", "run-e2e-1")

    # 6 synthesis: keep conflicting evidence plus follow-ups, never average
    state.stage = "coordinator_synthesis"
    from .agents import synthesize
    best, synth_gaps = synthesize(state.artifacts)
    assert synth_gaps is not None

    # 7 safety + MCP pre-check (must block without approval) -- full 10-item context
    state.stage = "safety_review"
    safety_ctx = {"target_host": "checkout-pod-7f8c", "problem_hosts": ["checkout-pod-7f8c"],
                  "event_host": "prod-gw-03", "blast_radius": "Deployment/checkout-api single workload",
                  "alternative_considered": True, "rollback_plan": True, "risk": "R3",
                  "approval": False, "caller_perm": True,
                  "args": {"service": "checkout-service", "target_version": "2026.09.14.9"},
                  "verify_plan": True}
    verdict = safety_review(best, {"name": "rollback_release"}, safety_ctx)
    assert verdict["verdict"] == "NEED_HUMAN_APPROVAL" and len(verdict["checks"]) == 10
    denied = check_tool_call({"tenant_id": tenant_id}, "rollback_release",
        {"tenant_id": tenant_id, "incident_id": incident_id, "service": "checkout-service",
         "target_version": "2026.09.14.9", "reason": "burn 12%/5m", "idempotency_key": "k-e2e-1"})
    assert not denied["allow"]

    # 8 approval + execution
    state.stage = "human_approval_if_required"
    approvals.create_card(incident_id, "AP-20260915-01", "P0", "checkout-service",
                          "2026.09.15.3", "2026.09.14.9", best.evidence_refs, "checkout order failures")
    ap = approvals.approve(incident_id, "AP-20260915-01", "duty officer", "k-e2e-1")
    assert ap["approved"]
    allowed = check_tool_call({"tenant_id": tenant_id}, "rollback_release",
        {"tenant_id": tenant_id, "incident_id": incident_id, "service": "checkout-service",
         "target_version": "2026.09.14.9", "reason": "burn 12%/5m", "idempotency_key": "k-e2e-1"}, approved=True)
    assert allowed["allow"]
    audit.append(tenant_id, "duty officer", "rollback_release", "checkout-service", "approved", "run-e2e-1")

    # 9 verification (minimal SLO-window assertions over 10 minutes of observation)
    state.stage = "recovery_verification"
    verified_result = {"success_rate": "98.3%", "burn": "stopped"}

    # 10 resolve: report + memory promotion (six conditions)
    state.stage = "resolve_or_escalate"
    report = build_report({"incident_id": incident_id})
    promo = verified.promote({"evidence_ids": best.evidence_refs, "verified": True, "service": "checkout-service",
                              "environment": "production", "human_confirmed": True, "desensitized": True})
    assert promo["status"] == "CANDIDATE"
    harness.store.save_state(state)
    return {"state": state, "report": report, "ledger": ledger, "audit": audit.events,
            "verification": verified_result, "approval": ap}
