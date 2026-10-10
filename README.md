# SRE Blackboard

A multi-agent incident copilot for Kubernetes microservices. When an SLO alert
fires, a coordinator agent plans an investigation, specialist agents query
metrics, logs, traces and cluster changes in parallel, and a synthesizer ranks
root-cause candidates. Every claim has to cite evidence. Any change to
production is proposed, checked by a deterministic policy engine, and needs
human approval before an isolated executor applies it.

> **Status: being rebuilt (milestone M0 of 10).** The first version was a
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
| HTTP ingress | FastAPI app with `GET /healthz` and `POST /api/v1/alerts` (validation, fingerprinting, 10-minute dedup). Dedup state is in-process, so run **one** worker. |
| Model gateway | Per-role model routing (default `claude-haiku-5-5`, configurable through `SRE_LLM_MODEL` / `SRE_LLM_ROLE_MODELS`), per-call and per-incident token budgets, and model pinning per incident. **No model is called yet** (M4). |
| Harness, agents, pipeline | Prototype control loop with a placeholder model that always finishes. To be replaced by a persisted state machine and real agents (M1, M4). |
| Queue and persistence | In-memory priority queue and in-memory sqlite. The prototype has known defects (low-priority starvation, priority lost on lease expiry), fixed in M1 by a Postgres-backed queue. |
| Safety and approvals | Prototype risk tiers and review checklist. Approval and audit are in-memory and unauthenticated, and no HTTP endpoint exposes them (M1, M5). |
| Retrieval | Heading-based chunking, BM25, RRF fusion and threshold filtering over 4 runbooks in `kb/`. The vector path is a placeholder (M6). |
| Evaluation | Prototype only; its metrics are self-confirming and are not reported (rewritten in M7). |
| Engineering | uv-managed project, ruff, mypy, pytest, pre-commit, GitHub Actions CI. |

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
make install     # uv sync + pre-commit hooks
make check       # ruff, mypy, pytest (the same checks CI runs)
make run         # API on http://localhost:8000
```

```bash
curl -s -X POST localhost:8000/api/v1/alerts -H 'content-type: application/json' \
  -d '{"alert_id":"a1","tenant_id":"t1","priority":"P0","service_id":"checkout",
       "alert_name":"HighErrorRate","environment":"prod","auth":{"tenant_id":"t1"}}'
```

Or run the API in a container with `docker compose up --build`.

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0 | Cleanup, honest docs, engineering baseline | in progress |
| M1 | Postgres, `SKIP LOCKED` job queue with leases and fencing, stateless API, auth | planned |
| M2 | kind + OpenTelemetry Demo, SLO alerts, fault injection (flagd, Chaos Mesh, bad rollouts) | planned |
| M3 | Read-only tools (Prometheus, Loki, Jaeger, Kubernetes), evidence store, record/replay | planned |
| M4 | Real agents: hand-written loop, pluggable LLM backends (`claude -p`, Anthropic API, replay) | planned |
| M5 | Policy engine, authenticated approvals, isolated executor, SLO recovery check, audit chain | planned |
| M9 | Minimal console (React), plus a GitHub Pages replay of a real run | planned |
| M7 | Labeled fault scenarios, live and replay eval, ablations, CI release gate | planned |
| M6 | Postgres hybrid retrieval (tsvector + pgvector, RRF, reranker) with a labeled query set | planned |
| M8 | OpenTelemetry traces and Prometheus metrics for the copilot itself | planned |

Rows are listed in execution order.

## Repository layout

```
app/            FastAPI ingress, gateway, prototype harness/agents/queue/safety/rag
kb/             4 self-written SRE runbooks (retrieval corpus)
tests/          pytest suite
eval/           prototype eval cases (to be replaced in M7)
fixtures/       prototype P0 alert fixture
docs/plan/      rebuild plan (Chinese)
docs/adr/       architecture decision records
docs/learn/     per-milestone study notes with interview Q&A (Chinese)
docs/legacy/    domain notes kept from the prototype
docs/index.html legacy GitHub Pages animation (scripted; replaced in M9)
```
