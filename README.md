# SRE Blackboard

*Intelligent SRE Collaboration Assistant*

A production-style, multi-agent incident copilot for cloud-native (Kubernetes)
microservices. Given a P0 alert, it collects metrics/log/trace/change/code
evidence, ranks root-cause candidates, and proposes a rollback -- while a Safety
agent plus human approval gates every risky action. The AI is a read-only
copilot: it never touches production write paths on its own.

> Note: the full executable specification (config contract, v2.0.0 baseline)
> is maintained locally and is intentionally not part of this repository.
> This README is the English-language project overview.

## How it works

Ten fixed workflow stages, no skipping:

```
normalize_and_deduplicate -> verify_impact -> coordinator_planning
-> initial_task_dispatch -> dynamic_evidence_collection
-> coordinator_synthesis -> safety_review
-> human_approval_if_required -> recovery_verification
-> resolve_or_escalate
```

Four alert types (`Problem / Event / Business / Host`) fan out from one
coordinator plan. Agents never create tasks and never hand off raw chat --
the only handoff is a versioned, evidence-linked **Artifact**.
`Problem Host` (root-cause candidate) and `Event Host` (observation point)
are tracked separately so a gateway 502 is never mistaken for the root cause.

## Architecture

| Layer | Module | Role |
|---|---|---|
| Control plane | `app/harness.py` | Owns flow, state, budgets, permissions, checkpoints, traces |
| Agent loop | `app/loop.py` | Observe -> act -> verify -> converge (max 5 steps) |
| Sandbox | `app/runtime.py` | Executes read-only tools, returns Observations |
| Roles | `app/agents.py` | Coordinator plan + per-capability agents |
| Skills | `app/skills/*` | Deterministic script chains per alert type |
| Knowledge | `app/knowledge.py`, `app/rag.py` | Chunking, dual-path recall, rerank, Knowledge MCP |
| Queue | `app/queue.py` | P0-first priority queues, 90s leases, retries, dead-letter |
| Storage | `app/persistence.py` | 4 core tables + experience tables (MySQL contract, sqlite locally) |
| Safety | `app/safety.py`, `app/auth.py`, `app/mcp.py` | R0-R4 tiers, 10-item review, RBAC+ABAC, approvals, audit chain |
| Budgets | `app/gateway.py`, `app/context.py` | Dual-model routing, token caps, billing |
| Memory | `app/memory.py` | Redis-style working memory + verified experience memory |
| Report | `app/reporter.py` | Fixed 10-section diagnosis report |
| Eval | `app/eval.py` | 100-sample frozen replay with release gates |
| Serve | `app/api.py`, `app/worker.py` | FastAPI ingress + Harness workers |

## Dual-model routing and budgets

| Role | Model | Cap |
|---|---|---|
| Coordinator / Safety / Code / Remediation | `muse-spark-1.3` (xhigh) | 64K per call |
| Triage / Metrics / Log / Trace / Host / ... / Reporter | `gemini-3.8-flash` (medium, triage uses low) | 32K per call |

Thinking tokens bill as output. A full P0 incident is budgeted at ~$0.12
(120K input / 18K output). Model IDs and tiers freeze per incident; mid-run
model switches are rejected. Context partitions (policy / metrics / logs /
trace / change-code-host-db / knowledge) cannot borrow from each other, and
raw logs, full traces, and whole-repo code never enter the prompt -- only
evidence IDs.

## Safety model

- **R0** read-only (automatic) / **R1** notifications/tickets (policy-automatic)
- **R2** restarts/scale-out (Safety + allowlist) / **R3** rollback/traffic/config/DB (human approval)
- **R4** delete data / bulk changes / network ACLs (permanently forbidden)
- 10-item Safety review: evidence, host match, event-host confusion, blast
  radius, lower-risk alternative, rollback plan, window/approval state,
  caller permission, injection check, verifiability.
- Approvals bind `incident_id + action_plan_id + idempotency key` with expiry;
  the audit log is hash-chained.

## Knowledge and retrieval

- KB: 4 self-authored SRE runbooks in `kb/` (checkout SLO triage, K8s
  release rollback, DB pool/cache, gateway 502 propagation).
- Chunking: h2/h3 split, 450 target / 200 min / 700 max tokens, 80 overlap,
  `chunk_id = knowledge_id:version:heading:ordinal`.
- Pipeline: mandatory tenant/permission filters -> BM25 Top20 + vector Top20
  -> RRF(k=60) Top30 -> rules + cross-encoder Top12 -> threshold 0.55 ->
  P0/P1 Top6. 300ms timeout degrades to rules with `reranker_degraded=true`.
- Reranker runs base-model + rules (`RERANKER_MODE=base_plus_rules`).
  Fine-tuning to `alert-reranker-qwen3-0.6b-v1` (QLoRA) is deferred until the
  1200-query labeling gate and blind-set MRR bar are met -- the code path
  already supports the swap.
- Knowledge citations always pair `chunk_id` with a live `evidence_id`;
  realtime evidence outranks retrieved docs.

## Evaluation

- `eval/cases_20.json`: 20 scenarios across P0-P3.
- `build_golden_100()` + `run_eval()`: 100 frozen samples covering the four
  alert types, priorities, tool failures, conflicts, approvals, high-risk
  actions. Release gates: cross-tenant hits, safety false passes, double
  claims, schema failures, and untraceable runs must all be zero.
- Report metrics the way the spec defines them: routing accuracy,
  high-risk recall, HitRate, MRR, overall accuracy.

## Quickstart

```bash
pip install -r requirements.txt
python -m pytest tests/ -q                      # 37 tests, no external services needed
python -m fixtures.make_p0                      # regenerate the P0 baseline fixture
python -c "from app.e2e import run_incident_e2e; import json; \
  print(run_incident_e2e(json.load(open('fixtures/p0_baseline.json')))['state'].stage)"
# -> resolve_or_escalate
```

Full local stack (self-hosted MySQL / Redis / Elasticsearch single-node):

```bash
docker compose up --build
```

Production wiring is URL-only: set `MYSQL_URL`, `REDIS_URL`, `ES_URL` to use
managed services. With no URLs set, everything falls back to local
same-contract stubs (`app/ext.py`), and the reranker stays in
base-plus-rules mode.

## Repo layout

```
app/            harness, agents, skills, rag, safety, eval, api, worker, ...
app/skills/     problem-alert / event-alert / business-alert / host-alert (+SKILL.md each)
kb/             4 SRE runbooks (retrieval corpus)
fixtures/       P0 baseline alert (spec Sec.2.4)
eval/           20 cases, 100-sample manifest, eval report output
tests/          37 contract/smoke/e2e tests
index.md        executable spec baseline (Chinese) -- the source of truth
docker-compose.yml  2 API workers + 2 harness workers + local mysql/redis/es
```

## Resume talking points

- Event-driven multi-agent Harness with blackboard, leases, checkpoints, and
  append-only traces -- not a prompt chain.
- Dual-model gateway with per-call hard caps, per-incident budgets (~$0.12/P0),
  and frozen model versions.
- Read-only-by-default safety: R3 human approval, 10-item review, hash-chained audit.
- Self-hosted retrieval with fine-tune-ready reranker behind a 300ms degrade budget.
- 100-sample frozen replay eval where release gates (not vibes) decide shipment.
