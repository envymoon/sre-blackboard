"""Persistence: 4 core tables. Maps to spec Sec.16.1; production uses MySQL,
local runs use sqlite3 with identical table and column names.

incidents/tasks/artifacts/run_traces all carry tenant_id/created_at/updated_at;
artifacts are immutable versions keyed by artifact_id+version; run_traces is
append-only; idempotency enforced by unique constraints.
If Redis is lost, the queues rebuild from here (rebuild only, facts never lost).
"""
from __future__ import annotations
import sqlite3
import time
import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents(incident_id TEXT PRIMARY KEY, tenant_id TEXT, alert_type TEXT, priority TEXT, status TEXT, alert_snapshot_json TEXT, final_summary_json TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, incident_id TEXT, tenant_id TEXT, skill_name TEXT, stage TEXT, priority TEXT, status TEXT, claimed_by TEXT, lease_until REAL, attempt INTEGER, input_json TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT, version TEXT, incident_id TEXT, task_id TEXT, tenant_id TEXT, artifact_type TEXT, producer TEXT, content_json TEXT, evidence_refs_json TEXT, created_at REAL, updated_at REAL, PRIMARY KEY(artifact_id, version));
CREATE TABLE IF NOT EXISTS run_traces(run_id TEXT PRIMARY KEY, incident_id TEXT, task_id TEXT, tenant_id TEXT, agent_name TEXT, step TEXT, tool_name TEXT, token_in INTEGER, token_out INTEGER, latency_ms INTEGER, status TEXT, created_at REAL, updated_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_idem ON tasks(incident_id, task_id);
CREATE TABLE IF NOT EXISTS experience_candidate(candidate_id TEXT PRIMARY KEY, incident_id TEXT, tenant_id TEXT, service TEXT, content_json TEXT, status TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS experience_memory(experience_id TEXT PRIMARY KEY, tenant_id TEXT, service TEXT, content_json TEXT, reviewer TEXT, created_at REAL, updated_at REAL);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def save_incident(self, incident_id: str, tenant_id: str, alert_type: str, priority: str, snapshot: dict):
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO incidents VALUES(?,?,?,?,?,?,?,?,?)",
            (incident_id, tenant_id, alert_type, priority, "OPEN", json.dumps(snapshot, ensure_ascii=False), "", now, now))
        self.db.commit()

    def save_task(self, task_id: str, incident_id: str, tenant_id: str, skill: str, priority: str, status: str = "READY"):
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, incident_id, tenant_id, skill, "READY", priority, status, "", 0.0, 0, "{}", now, now))
        self.db.commit()

    def save_artifact(self, artifact_id: str, incident_id: str, task_id: str, tenant_id: str, content: dict, evidence: list, version: str = "v1"):
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (artifact_id, version, incident_id, task_id, tenant_id, content.get("artifact_type", ""),
             content.get("producer", ""), json.dumps(content, ensure_ascii=False), json.dumps(evidence), now, now))
        self.db.commit()

    def append_trace(self, run_id: str, incident_id: str, task_id: str, tenant_id: str, agent: str, tool: str, tin: int, tout: int, latency: int, status: str):
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO run_traces VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, incident_id, task_id, tenant_id, agent, "", tool, tin, tout, latency, status, now, now))
        self.db.commit()

    def ready_tasks(self):
        return list(self.db.execute("SELECT task_id,incident_id,priority FROM tasks WHERE status='READY' ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END"))

    def rebuild_queue(self, queue) -> int:
        """Rebuild the four queues from MySQL READY rows after Redis loss, P0 first. Returns the rebuild count."""
        from .queue import make_message
        rows = self.ready_tasks()
        for task_id, incident_id, priority in rows:
            queue.enqueue(make_message(incident_id, task_id, "Problem", priority))
        return len(rows)

    def save_candidate(self, candidate_id: str, incident_id: str, tenant_id: str, service: str, content: dict):
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO experience_candidate VALUES(?,?,?,?,?,?,?,?)",
            (candidate_id, incident_id, tenant_id, service, json.dumps(content, ensure_ascii=False), "candidate", now, now))
        self.db.commit()

    def publish_experience(self, experience_id: str, tenant_id: str, service: str, content: dict, reviewer: str):
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO experience_memory VALUES(?,?,?,?,?,?,?)",
            (experience_id, tenant_id, service, json.dumps(content, ensure_ascii=False), reviewer, now, now))
        self.db.commit()

    def load_incident_state(self, incident_id: str):
        """Blackboard recovery: rebuild an IncidentState (tasks + artifact refs) from MySQL. Maps to spec Sec.20.1."""
        from .models import IncidentState, Task, Artifact
        row = self.db.execute("SELECT priority FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        state = IncidentState(incident_id=incident_id)
        for task_id, skill, priority, status in self.db.execute(
                "SELECT task_id,skill_name,priority,status FROM tasks WHERE incident_id=?", (incident_id,)):
            state.tasks[task_id] = Task(task_id=task_id, incident_id=incident_id, alert_type="Problem",
                                        priority=priority or (row[0] if row else "P0"), capability="logs",
                                        status=status or "READY")
        for aid, ver, tid, atype, prod, evj in self.db.execute(
                "SELECT artifact_id,version,task_id,artifact_type,producer,evidence_refs_json FROM artifacts WHERE incident_id=?", (incident_id,)):
            state.artifacts.append(Artifact(incident_id=incident_id, producer=prod or "", artifact_type=atype or "",
                                            evidence_refs=json.loads(evj or "[]")))
        return state
