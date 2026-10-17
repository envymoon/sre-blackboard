# SRE Blackboard

A multi-agent incident copilot for Kubernetes microservices. When an SLO alert
fires, a coordinator agent plans an investigation, specialist agents query
metrics, logs, traces and cluster changes in parallel, and a synthesizer ranks
root-cause candidates. Every claim has to cite evidence. Any change to
production is proposed, checked by a deterministic policy engine, and needs
human approval before an isolated executor applies it.

> **Status: being rebuilt (M0–M5 done; M9 next).** The first version was a
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
| Storage | Postgres 16 with Alembic migrations: `incidents`, `alerts`, `jobs`, `audit_log`, `evidence`, `tool_calls`, for the agents `llm_calls`, `agent_runs`, `blackboard`, and for remediation `remediation_plans`, `approvals`, `executions`. One transaction writes the alert, opens or merges the incident, enqueues its triage job and appends the audit event. |
| Deduplication | A partial unique index allows one open incident per (tenant, fingerprint). Concurrent duplicates merge; a test sends 50 at once and gets one incident. |
| Job queue | `FOR UPDATE SKIP LOCKED` claims, priority aging (a waiting P3 eventually beats new P0s), a slot reserved for P0, leases with heartbeats, a reaper that keeps priority, fencing tokens, exponential backoff, dead letters. |
| Worker | `python -m app.worker`: runs handlers, cancels a handler whose lease was taken, and on SIGTERM drains or hands back its jobs. Triage moves an incident from `RECEIVED` to `TRIAGED` and queues its investigation (`SRE_AUTO_INVESTIGATE=false` stops at triage). |
| Incident state machine | Explicit transition table, optimistic locking on `version`, every transition audited. |
| Audit log | Full SHA-256 hash chain; a trigger blocks UPDATE and DELETE. `python -m app.db.audit verify` (`make audit-verify`) and `GET /api/v1/audit/verify` (admin) recompute the chain and name the first tampered row. Every remediation step is an event: plan, approval or rejection, execution started and finished, verification, and each state transition. |
| LLM backends (M4) | One interface, "given the transcript, what is the next step": at most 3 tool calls or a final answer ([ADR-0006](docs/adr/0006-agent-loop-llm-backends-blackboard.md)). `cli` runs one `claude -p` per step on the Claude subscription, with every built-in tool, MCP server and local setting turned off and the output constrained by a JSON schema, so the model can only propose. `api` uses the Anthropic Messages API with native tool use and prompt caching. `replay` answers from a recording. Per-role model routing (default `claude-haiku-5-5`), retries with backoff for rate limits and overloads, and at most 2 `claude -p` processes per worker. |
| Agents (M4) | A hand-written loop in `app/agents/`. A coordinator writes a plan; three investigators (metrics, logs and traces, changes) work in parallel with only their own tools, up to 8 steps each; a synthesizer ranks root-cause candidates and separates facts, inferences and open hypotheses. Every cited evidence ID is checked without a model: it must belong to the incident, have been seen by the agent that cites it, and match the claim's kind. The model gets one chance to fix a bad citation, then the claim is dropped; with no supported root cause the incident is `ESCALATED`, as it is when the token budget runs out. Each agent's result and its "done" mark are written in one fenced transaction, so a worker killed mid-investigation is taken over without asking the model again for finished agents (a test SIGKILLs a real worker process). `GET /api/v1/incidents/{id}/diagnosis` returns the structured diagnosis, `/report` a Markdown report, and `GET /api/v1/evidence/{id}` the raw data behind any citation. A whole investigation can be exported and replayed in CI without a model or a cluster. On the lab with `claude-haiku-5-5`, the top root cause was right for all 3 acceptance faults in the last 2 of 3 runs (2/3 in the first), at about $0.05 per investigation; the last run's 8 investigations are replayed in CI. The weak spot: the incident on the delayed service itself tends to blame its database, whose client spans slow down with it ([study note](docs/learn/m4-agent.md#4-验收结果)). |
| Remediation (M5) | After a diagnosis a fifth agent, the planner, proposes at most two actions from a catalogue of three: `rollout_undo` (R3), `restart_deployment` and `scale_deployment` (R2), each citing evidence and naming the alternatives it rejected ([ADR-0007](docs/adr/0007-remediation-policy-approval-executor.md)). It can only read rollout history, deployment diffs and Kubernetes events. |
| Policy engine (M5) | `app/remediation/policy.py`, a pure function over structured fields only; the model's prose never reaches it. Blocking checks: the action is in the catalogue; the target is in the allowlist (`SRE_REMEDIATION_TARGETS`, empty means none); the target is the diagnosis's top root cause with confidence ≥ 0.5, not merely where the alert fired (problem host vs. event host); a rollback cites a rollout history or diff of that same Deployment; a way back exists; the change window is open. The verdict is `deny` with reasons if any fails, `allow` up to `SRE_REMEDIATION_AUTO_MAX_RISK` (default R1, so every current action needs approval), `require_approval` above. Any exception is a `deny`. |
| Approvals (M5) | `POST /api/v1/incidents/{id}/approvals` needs the `approver` role; the approver comes from the token, must not be whoever proposed the plan, and must send the plan's SHA-256 `plan_hash`. A revised plan (`POST /incidents/{id}/plans`, responder) gets a new hash, so an old approval never carries over. Plans and approvals are append-only. A plan still waiting after `SRE_REMEDIATION_APPROVAL_TTL_S` (30 minutes) escalates the incident. |
| Executor (M5) | `python -m app.executor`, a separate process that claims only `execute` jobs, with its own ServiceAccount that can get, list and patch Deployments and read ReplicaSets in one namespace, and no model or reader token. Before acting it re-runs the policy and re-checks the approval and its hash. It sends a server-side dry run, records the execution under a unique idempotency key, then applies a JSON patch guarded by the Deployment's `resourceVersion`. Run at most once: a redelivered job that finds its execution already started marks it `unknown` and escalates instead of trying again. |
| Verification (M5) | After the rollout, the worker reads the alert's SLI straight from Prometheus over 1 minute (error ratio below 6%, or p95 below 1 s); three healthy reads in a row resolve the incident, and if the deadline passes it is escalated with how to undo the change. On the lab, the bad release closed the whole loop: alert, diagnosis (checkout's release), a proposed `rollout_undo checkout` (R3, so `require_approval`), approval through the API, rollback from revision 2 to 3, the error ratio back to 0, and `RESOLVED` 130 s after the approval, with the audit chain intact. For the feature-flag and network faults, which no catalogue action repairs, the planner proposed no action in 6 of 7 incidents and wrote down what a human should check instead ([study note](docs/learn/m5-safety.md#4-验收结果)). |
| Prompt injection (M5) | Tool output stays in a data section the prompts say is not instructions; the policy reads no prose; the executor accepts only catalogue actions. `scripts/injection_eval.py` plants instructions in a rollout's change-cause and in a quoted log line and runs the planner with the real model on every `Model check` run: 0 safety violations, and the model ignored both injections. |
| Prototype modules | Retrieval from the first version is still in `app/` (`rag.py`, `knowledge.py`) and is not wired into the API or worker. It is replaced in M6. |
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
make lab-workers                     # point the compose workers at the lab (needs a Claude login, below)
make lab-accept                      # the acceptance run (alerts, tool checks, diagnoses, the approved rollback)
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

### Agents

The workers call the model through `claude -p`, so they need a Claude Code login. Put a token
from `claude setup-token` into `CLAUDE_CODE_OAUTH_TOKEN` in the environment `make up` runs in
(never into a committed file), or set `SRE_LLM_BACKEND=api` and `SRE_ANTHROPIC_API_KEY`.

```bash
make llm-smoke                       # one real agent step; prints tokens and cost
make llm-probe                       # final answers, tool isolation, error shapes
# after an alert: the incident goes TRIAGED -> INVESTIGATING -> DIAGNOSED (or ESCALATED)
curl -s localhost:8000/api/v1/incidents/$ID/report -H "authorization: Bearer $(make -s token ROLES=viewer TENANT=t1)"
# then PLANNED -> SAFETY_REVIEWED -> AWAITING_APPROVAL; approve the plan's hash as someone else
curl -s localhost:8000/api/v1/incidents/$ID/plans -H "authorization: Bearer $(make -s token ROLES=viewer TENANT=t1)"
curl -s -X POST localhost:8000/api/v1/incidents/$ID/approvals -H 'content-type: application/json' \
  -H "authorization: Bearer $(make -s token ROLES=approver TENANT=t1)" -d '{"plan_hash": "…", "decision": "approve"}'
# the executor applies it (make lab-workers starts it), then EXECUTING -> VERIFYING -> RESOLVED
make audit-verify
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
| M4 | Real agents: hand-written loop, pluggable LLM backends (`claude -p`, Anthropic API, replay) | done |
| M5 | Policy engine, authenticated approvals, isolated executor, SLO recovery check, audit chain | done |
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
app/llm/        LLM backends (claude -p, Anthropic API, replay), retries, per-role routing
app/agents/     agent loop, prompts, citation checks, blackboard store, investigation, report, recordings
app/remediation/ action catalogue, plans, policy engine, planner agent, approval flow, SLO checks
app/executor/   the executor process: Kubernetes writer, execute job
app/*.py        prototype rag/knowledge (being replaced in M6)
kb/             4 self-written SRE runbooks (retrieval corpus)
tests/          pytest suite; tests/fixtures/{lab,agent}: real lab recordings for the contract tests
deploy/kind/    fault lab: kind config, pinned versions, chart values, SLO rules and their tests
scripts/        M1 load test, LLM probe; scripts/lab: fault injection CLI, acceptance, tool checks, diagnosis scoring
eval/           prototype eval cases (to be replaced in M7)
docs/plan/      rebuild plan (Chinese)
docs/adr/       architecture decision records
docs/learn/     per-milestone study notes with interview Q&A (Chinese)
docs/legacy/    domain notes kept from the prototype
docs/index.html legacy GitHub Pages animation (scripted; replaced in M9)
```
