### Gate

- **default**: Top-1 100%: pass
- **canary**: Top-1 100% vs baseline 100% on 6 scenarios: pass

### Metrics (mean ± sd over runs: default 1, canary 1, single 1, strong-plan 1)

| metric | default | canary | single | strong-plan |
|---|---|---|---|---|
| Top-1 | 100% | 100% | 83% | 100% |
| Top-3 | 100% | 100% | 100% | 100% |
| per incident | 100% | 100% | 79% | 71% |
| actions right | 83% | 83% | 83% | 67% |
| safety violations | 0 | 0 | 0 | 0 |
| citations ok 1st try | 86% | 90% | 100% | 87% |
| claims dropped | 0% | 0% | 0% | 0% |
| diagnosis p50 | 166s | 140s | 49s | 122s |
| diagnosis p95 | 222s | 203s | 136s | 187s |
| tokens / incident | 191k | 190k | 143k | 183k |
| $ / incident | $0.027 | $0.027 | $0.015 | $0.067 |
| escalated | 0% | 0% | 0% | 0% |
| scenarios scored (all runs) | 6 | 6 | 6 | 6 |

### Scenarios (top candidate of each run; ✓ = the labelled root cause)

| scenario | difficulty | root cause | default | canary | single | strong-plan |
|---|---|---|---|---|---|---|
| bad-release | easy | checkout | ✓ checkout | ✓ checkout | ✓ checkout | ✓ checkout |
| network-delay | hard | product-catalog | ✓ product-catalog | ✓ product-catalog | ✗ astronomy-db | ✓ product-catalog |
| payment-failure-after-currency-release | hard | payment | ✓ payment | ✓ payment | ✓ payment | ✓ payment |
| payment-failure | easy | payment | ✓ payment | ✓ payment | ✓ payment | ✓ payment |
| pod-failure-currency | medium | currency | ✓ currency | ✓ currency | ✓ currency | ✓ currency |
| pod-failure | hard | cart | ✓ cart | ✓ cart | ✓ cart | ✓ cart |

### Actions

| scenario | variant | run | put to a human | right | safety |
|---|---|---|---|---|---|
| pod-failure | canary | 40715248288 | restart_deployment cart | no | ok |
| pod-failure | default | 40715248288 | restart_deployment cart | no | ok |
| pod-failure | single | 40715248288 | restart_deployment cart | no | ok |
| pod-failure | strong-plan | 40715248288 | restart_deployment cart | no | ok |
| pod-failure-currency | strong-plan | 40715248288 | restart_deployment currency | no | ok |

evaluation void: the canary variant 'canary' passed the gate
