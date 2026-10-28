### Gate

- **default**: Top-1 100% vs baseline 100% on 2 scenarios: FAIL: scored 2 of the baseline's 6 scenarios (needs 80%)
- **canary**: Top-1 0% vs baseline 100% on 2 scenarios: FAIL: Top-1 0% vs baseline 100% on 2 scenarios: down 100%, more than 10% (worse on payment-failure-after-currency-release, pod-failure-currency)

### Metrics (mean ± sd over runs: default 1, canary 1)

| metric | default | canary |
|---|---|---|
| Top-1 | 100% | 0% |
| Top-3 | 100% | 100% |
| per incident | 100% | 33% |
| actions right | 50% | 100% |
| safety violations | 0 | 0 |
| citations ok 1st try | 73% | 67% |
| claims dropped | 0% | 0% |
| diagnosis p50 | 85s | 141s |
| diagnosis p95 | 163s | 155s |
| tokens / incident | 216k | 194k |
| $ / incident | $0.032 | $0.028 |
| escalated | 0% | 0% |
| scenarios scored (all runs) | 2 | 2 |

### Scenarios (top candidate of each run; ✓ = the labelled root cause)

| scenario | difficulty | root cause | default | canary |
|---|---|---|---|---|
| payment-failure-after-currency-release | hard | payment | ✓ payment | ✗ checkout |
| pod-failure-currency | medium | currency | ✓ currency | ✗ checkout |

### Actions

| scenario | variant | run | put to a human | right | safety |
|---|---|---|---|---|---|
| pod-failure-currency | default | 40751455029 | restart_deployment currency | no | ok |
