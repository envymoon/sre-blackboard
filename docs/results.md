# Results and where they come from

Every number this repository claims, with the run, file or test it was measured in and how to
check it again. Numbers marked **CI** are recomputed on every pull request without a model;
the others come from lab runs on GitHub Actions that use the Claude subscription, and only
their recordings are re-checked in CI. Study notes are in Chinese.

## Queue, API and storage (M1)

| Claim | Measured in | Check it |
|---|---|---|
| 2000 alerts, 200 fingerprints, 100 concurrent: 0 failures, 200 incidents, 200 triage jobs done, max fence 1, audit chain intact | load test against `docker compose` (2 API processes, 2 workers); [m1 note §4](learn/m1-postgres-queue.md#4-验收结果) | **CI** `e2e`: `scripts/loadtest.py`, then `scripts/obs_check.py` reconciles the same numbers in Prometheus |
| about 230 alerts/s through the compose stack; `docker compose down` in 1.5 s with graceful worker exit | same, 2026-10-14 | `make up && make loadtest` |
| 50 concurrent duplicates merge into one incident | `tests/test_api.py` | **CI** `check` |
| a SIGKILLed worker's job is taken over after its lease; a waiting P3 eventually beats new P0s | `tests/test_queue.py`, `tests/test_worker.py` | **CI** `check` |
| tampering with any audit row is found and named | `tests/test_audit_and_states.py`; `make audit-verify` | **CI** `check` |

## Fault lab (M2)

| Claim | Measured in | Check it |
|---|---|---|
| 3 representative faults each page within 5 minutes (first alert 128–221 s over runs 5–8) and clear after recovery; 10-minute baseline with no alert | lab run 8, 2026-10-15; [m2 note §4](learn/m2-fault-lab.md#4-验收结果) | `Fault lab` workflow (uses no model unless agents are on) |
| SLO burn-rate rules behave as specified | `deploy/kind/rules/tests/slo_test.yml` | **CI** `check` (`make rules-test`) |

## Tools and evidence (M3)

| Claim | Measured in | Check it |
|---|---|---|
| 16/16 tool checks against live faults | lab run 11; [m3 note §4](learn/m3-tools.md#4-验收结果) | recordings in `tests/fixtures/lab/` |
| replay returns byte-identical results and evidence ids | `tests/test_tool_contracts.py` on the lab's recordings | **CI** `check` |
| what the model sees is 25–600× smaller than the raw trace and log results | same recordings, m3 note §4 | `tests/test_tool_contracts.py` asserts under a tenth |

## Agents (M4) and remediation (M5)

| Claim | Measured in | Check it |
|---|---|---|
| top root cause right for 3/3 acceptance faults (runs 16 and 17), 2/3 in run 15 | lab runs 15–17; [m4 note §4](learn/m4-agent.md#4-验收结果) | the 8 investigations of the last lab run replay with zero misses: `tests/test_agent_contracts.py`, **CI** |
| a whole investigation costs about $0.025–0.06 (API-price equivalent, not a bill) | `llm_calls.cost_usd` from the CLI's `total_cost_usd` | same |
| bad release closed end to end: alert, diagnosis, `rollout_undo checkout` (R3), approval by hash, rollback, SLO recovered, `RESOLVED` 130 s after the approval; 88-row audit chain verified | lab run 18; [m5 note §4](learn/m5-safety.md#4-验收结果) | the run's recordings: `tests/test_agent_contracts.py` (8 investigations and 8 plans) |
| for faults no catalogue action fixes, the planner proposed no action in 6 of 7 incidents | same run | same |
| 0 safety violations on planted prompt injections | `scripts/injection_eval.py` with the real model | `Model check` workflow (uses the model) |
| the policy engine denies anything outside the catalogue, allowlist or evidence | `tests/test_policy.py`, `tests/test_executor.py` | **CI** `check` |

## Console (M9)

| Claim | Measured in | Check it |
|---|---|---|
| first-load JS 111–157 KB gzip across the four entry pages | `npm run budget` | **CI** `web` and the Pages build |
| Lighthouse performance 91–94 mobile, 100 desktop; CLS ≤ 0.028; slowest interaction 88 ms at 4× CPU slowdown | Lighthouse 12.8, local; [m9 note §4](learn/m9-console.md#4-验收结果) | rerun Lighthouse on the Pages site |
| text contrast ≥ 4.5:1 for 38 pairs in both themes | `web/src/styles/tokens.test.ts` | **CI** `web` |
| the replayed case is a real incident: alert 08:06:18, diagnosis 08:08:56 (checkout 80%), approval 08:09:13, `RESOLVED` 08:11:16; 23 model calls, 456k tokens, $0.058 | lab run 40465726955, exported to `web/public/cases/bad-release.json` | the [demo GIF](assets/demo-bad-release.gif) is this file played back (`web/scripts/record-demo.mjs`) |

## Evaluation (M7)

| Claim | Measured in | Check it |
|---|---|---|
| default agents: Top-1 6/6 on the 6 core scenarios, 0 safety violations, about 191k tokens and $0.027 per incident, diagnosis p50 166 s / p95 222 s | Evaluation run 40715248288, one run per variant; `eval/results/run-40715248288.json` | `uv run python -m app.eval report eval/results/*.json` |
| single agent: Top-1 5/6, p50 49 s, $0.015; strong-plan: 6/6 at 2.5× the cost | same run | same |
| a deliberately wrong synthesizer prompt scores Top-1 0/2 and the gate fails it | Evaluation run 40751455029 | **CI** `check`: `tests/test_eval_replay.py` replays every recording and applies the gate |
| 22 scenarios labelled from the injection point before any run | `eval/scenarios/*.yaml` | `tests/test_scenarios.py` checks each label against the fault catalogue, **CI** |

One run per configuration on 6 scenarios: one scenario is 17 points, so only large differences
are signal.

## Knowledge base (M6)

| Claim | Measured in | Check it |
|---|---|---|
| hybrid + rerank on 54 answerable labelled queries: hit@1 0.741, hit@5 0.981, MRR 0.841; each stage (full text, vector, RRF, rerank) improves on the previous | `eval/retrieval/report.json` | **CI** `retrieval`: recomputed with the real local models on every pull request |
| the threshold empties all 8 unanswerable queries, and also 22% of answerable ones (paraphrases) on the held-out half | same | same |

## The system's own observability (M8)

| Claim | Measured in | Check it |
|---|---|---|
| metrics reconcile with the load test in a real Prometheus; 11 rules healthy; every rule and dashboard query evaluates | `scripts/obs_check.py` | **CI** `e2e` |
| one trace per incident across processes: API root span, worker's `job triage` | same, in a real Jaeger | **CI** `e2e` |
| the replayed bad-release investigation is a 71-span tree (3 jobs, 6 agents, 21 model calls, 39 tool calls, the policy) | `obs_check.py --replay` | **CI** `e2e` |
| about 37 µs per span, 4 µs per counter and histogram | local microbenchmark; [m8 note](learn/m8-observability.md) | — |
| alert rules on the system itself | `deploy/observability/rules/tests/` | **CI** `check` |

## Not shown yet

These are stated as gaps wherever they come up; the runs cost subscription quota and wait for it.

- **Accuracy is from small samples.** One run per variant on 6 of the 22 scenarios; three runs
  per configuration and all scenarios have not been run.
- **The 2-minute diagnosis objective is not met.** Default p50 is 166 s.
- **The knowledge base has no live result.** Retrieval quality is measured; whether the `rag`
  variant diagnoses better is not.
- **Live traces are missing.** No lab run has shown where the 166 s go, agent by agent.
- **Remediation closed the loop once.** That was one fault in one run, with a script as the approver. The
  failure paths (execution error, verification timeout, expired approval, executor killed
  mid-change) are covered by tests with fake Kubernetes and Prometheus, not triggered in the lab.
