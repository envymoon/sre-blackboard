# SRE Blackboard

A multi-agent incident copilot for Kubernetes microservices. When an SLO alert
fires, a coordinator agent plans an investigation, specialist agents query
metrics, logs, traces and cluster changes in parallel, and a synthesizer ranks
root-cause candidates. Every claim has to cite evidence. Any change to
production is proposed, checked by a deterministic policy engine, and needs
human approval before an isolated executor applies it.

> **Status: being rebuilt (M0–M3 done; M4 next).** The first version was a
> spec-shaped prototype: it had no real model calls, no real tool backends, and
> an evaluation that could not fail. This repository is now being rebuilt into a
> working system, one milestone at a time. The plan, including every known
> defect and how it gets fixed, is in
> [`docs/plan/rebuild-plan.md`](docs/plan/rebuild-plan.md) (Chinese).
> This README only describes what exists on `main`.

## Design principles

These carry over from the prototype and are what the rebuild implements for real:

- **Blackboard handoff.** Agents never chat with each other. They publish
  versioned, structured findings to a shared blackboard, and each finding
  references the evidence it rests on.
- **Evidence or it didn't happen.** A finding may only cite evidence IDs that
  this incident's tool calls actually produced. Knowledge-base citations never
  stand alone; they must be paired with live evidence.
- **Problem host vs. event host.** The service where symptoms show up (for
  example a gateway returning 502s) is tracked separately from the
  root-cause candidate, so remediation is never aimed at the messenger.
- **Read-only AI.** Agents have no write tools. Actions are tiered R0–R4. R3
  (rollback, traffic shift, config change) needs human approval bound to the
  exact plan, and R4 is never allowed.
- **Budgets everywhere.** Per-call and per-incident token limits, step limits
  per agent, and timeouts per tool. If a budget runs out, the incident is
  escalated together with the evidence gathered so far.

## What exists today

| Area | State on `main` |
|---|---|
| HTTP API | Stateless FastAPI app. `POST /api/v1/alerts` takes Alertmanager webhooks; `GET /api/v1/incidents[/{id}]` reads incidents; `/healthz` (liveness) and `/readyz` (database). Run as many processes as you like. |
| Auth | HS256 JWT with roles `viewer < responder < approver < admin`, plus an `ingest` service role for alert sources. The tenant comes only from the token; tokens without one are rejected. CORS is an explicit allowlist. |
| Storage | Postgres 16 with Alembic migrations: `incidents`, `alerts`, `jobs`, `audit_log`, `evidence`, `tool_calls`. One transaction writes the alert, opens or merges the incident, enqueues its triage job and appends the audit event. |
| Deduplication | A partial unique index allows one open incident per (tenant, fingerprint). Concurrent duplicates merge; a test sends 50 at once and gets one incident. |
| Job queue | `FOR UPDATE SKIP LOCKED` claims, priority aging (a waiting P3 eventually beats new P0s), a slot reserved for P0, leases with heartbeats, a reaper that keeps priority, fencing tokens, exponential backoff, dead letters. |
| Worker | `python -m app.worker`: runs handlers, cancels a handler whose lease was taken, and on SIGTERM drains or hands back its jobs. The only handler so far moves an incident from `RECEIVED` to `TRIAGED`; investigation arrives in M4. |
| Incident state machine | Explicit transition table, optimistic locking on `version`, every transition audited. |
| Audit log | Full SHA-256 hash chain; a trigger blocks UPDATE and DELETE; `verify` finds the first tampered row. |
| Model gateway | Per-role model routing (default `claude-haiku-5-5`) and token budgets. **No model is called yet** (M4). |
| Prototype modules | Harness loop, agents, safety checks, retrieval and eval from the first version are still in `app/` and are not wired into the API or worker. They are replaced in M4–M7. |
| Fault lab (M2) | `make env-up` builds a kind cluster with the OpenTelemetry Demo (trimmed), Loki, Chaos Mesh and SLO burn-rate alerts (promtool-tested); Alertmanager posts to this API. `make fault SCENARIO=…` injects one of 15 faults (feature flags, Chaos Mesh, a bad rollout). The lab is rebuilt from scratch and checked on GitHub Actions ([ADR-0004](docs/adr/0004-fault-lab-on-kind-in-ci.md)): a payment failure, a network delay and a bad release each reach this API as alerts within about 2–4 minutes and resolve after recovery, on a lab that first stays alert-free for 10 minutes. The whole lab uses about 4.7 GiB. |
| Tools (M3) | Nine read-only tools in `app/tools/` over Prometheus, Loki, Jaeger, the Kubernetes API and flagd ([ADR-0005](docs/adr/0005-tool-layer-evidence-and-replay.md)). Each has a strict Pydantic schema that doubles as the model's tool definition, a timeout, retries for transient errors and a circuit breaker per backend; failures come back as structured errors with the backend's message. The model gets a bounded summary (counts, top-N, peaks, log templates) and an evidence ID; the raw result is stored in `evidence` with its SHA-256, and rows cannot be updated or deleted. Evidence IDs are derived from the content, so a replay shows the model the same bytes. Calls can be recorded to a cassette and replayed without a cluster. Kubernetes access is a ServiceAccount that can only read in the demo namespace; Chaos Mesh objects are filtered out so the lab never hands the agent the answer. On the lab, during each of the 3 acceptance faults, the tools return the data a diagnosis needs (the failing service's error ratio, the bad revision and its diff, the slow spans); CI replays those real recordings and must reproduce the live results exactly. |
| Engineering | uv, ruff, mypy, pytest (database tests run against a real Postgres via testcontainers), pre-commit, GitHub Actions CI. |

## Quickstart

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
make install                 # uv sync + pre-commit hooks
make check                   # ruff, mypy, pytest (the same checks CI runs)

cp .env.example .env         # then set SRE_JWT_SECRET (32+ characters)
make up                      # Postgres, migrations, API (2 processes), 2 workers
make loadtest                # 1000 duplicate alerts, then checks nothing was duplicated
```

Send an alert by hand:

```bash
TOKEN=$(make -s token ROLES=ingest TENANT=t1)
curl -s -X POST localhost:8000/api/v1/alerts -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"status": "firing", "alerts": [{"status": "firing",
  "labels": {"alertname": "HighErrorRate", "service": "checkout", "environment": "prod", "severity": "critical"}}]}'
curl -s localhost:8000/api/v1/incidents -H "authorization: Bearer $(make -s token ROLES=viewer TENANT=t1)"
```

To run the API and a worker on the host instead: `make db migrate`, then `make run` and `make worker`.

### Fault lab

Needs kind, kubectl and helm as well, and about 10 GB of memory for Docker.

```bash
make up                              # the API must be running: Alertmanager posts to it
make env-up                          # kind cluster + demo + alerting (first run pulls ~3 GB)
make faults                          # list what can be broken
make fault SCENARIO=payment-failure  # break something; an alert reaches the API within minutes
make lab-status                      # active faults and firing alerts
make recover ALL=1                   # undo everything
make lab-accept                      # the acceptance run (alerts, plus the tool checks)
make env-down
```

The read-only tools can be called by hand against the lab:

```bash
make lab-env                         # endpoints and a 12h read-only token, into .lab.env
uv run python -m app.tools list      # tool definitions as the model sees them
uv run python -m app.tools call prom_query '{"query": "service:sli_error_ratio:rate5m"}'
uv run python -m app.tools call deploy_diff '{"deployment": "checkout"}' --record lab.jsonl.gz
uv run python -m app.tools call deploy_diff '{"deployment": "checkout"}' --replay lab.jsonl.gz
```

The demo shop is at http://localhost:18080 (Jaeger under `/jaeger/ui`, feature flags under
`/feature`), Prometheus at :19090, Alertmanager at :19093, Loki at :13100.

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0 | Cleanup, honest docs, engineering baseline | done (`v0.1.0`) |
| M1 | Postgres, `SKIP LOCKED` job queue with leases and fencing, stateless API, auth | done |
| M2 | kind + OpenTelemetry Demo, SLO alerts, fault injection (flagd, Chaos Mesh, bad rollouts) | done |
| M3 | Read-only tools (Prometheus, Loki, Jaeger, Kubernetes), evidence store, record/replay | done |
| M4 | Real agents: hand-written loop, pluggable LLM backends (`claude -p`, Anthropic API, replay) | planned |
| M5 | Policy engine, authenticated approvals, isolated executor, SLO recovery check, audit chain | planned |
| M9 | Minimal console (React), plus a GitHub Pages replay of a real run | planned |
| M7 | Labeled fault scenarios, live and replay eval, ablations, CI release gate | planned |
| M6 | Postgres hybrid retrieval (tsvector + pgvector, RRF, reranker) with a labeled query set | planned |
| M8 | OpenTelemetry traces and Prometheus metrics for the copilot itself | planned |

Rows are listed in execution order.

## Repository layout

```
app/api/        FastAPI app factory and routes (alerts, incidents, health)
app/core/       settings, authentication and roles
app/db/         table definitions, Alembic migrations, incident and audit repositories
app/queue/      the Postgres job queue
app/worker/     worker loop and job handlers
app/ingest.py   Alertmanager webhook -> alerts, incidents, triage jobs
app/tools/      read-only tools, evidence store, record/replay (python -m app.tools)
app/*.py        prototype harness/agents/safety/rag/eval (being replaced)
kb/             4 self-written SRE runbooks (retrieval corpus)
tests/          pytest suite; tests/fixtures/lab: real lab recordings for the tool contract tests
deploy/kind/    fault lab: kind config, pinned versions, chart values, SLO rules and their tests
scripts/        M1 load test; scripts/lab: fault injection CLI, acceptance and tool checks
eval/           prototype eval cases (to be replaced in M7)
fixtures/       prototype P0 alert fixture
docs/plan/      rebuild plan (Chinese)
docs/adr/       architecture decision records
docs/learn/     per-milestone study notes with interview Q&A (Chinese)
docs/legacy/    domain notes kept from the prototype
docs/index.html legacy GitHub Pages animation (scripted; replaced in M9)
```
