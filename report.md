### Gate

- **default**: Top-1 90%: pass

### Metrics (mean ± sd over runs: default 1)

| metric | default |
|---|---|
| Top-1 | 90% |
| Top-3 | 90% |
| per incident | 93% |
| actions right | 80% |
| safety violations | 0 |
| citations ok 1st try | 80% |
| claims dropped | 0% |
| diagnosis p50 | 165s |
| diagnosis p95 | 238s |
| tokens / incident | 326k |
| $ / incident | $0.047 |
| escalated | 0% |
| scenarios scored (all runs) | 10 |

### Scenarios (top candidate of each run; ✓ = the labelled root cause)

| scenario | difficulty | root cause | default |
|---|---|---|---|
| ad-failure | easy | ad | - no alert |
| ad-high-cpu | medium | ad | - no alert |
| bad-release | easy | checkout | ✓ checkout |
| cart-failure | medium | cart | - no alert |
| cart-readiness-failure | hard | cart | - no alert |
| cpu-stress | medium | recommendation | - no alert |
| email-memory-leak | medium | email | - no alert |
| frontend-bad-release | hard | frontend | ✗ cart |
| intl-shipping-slowdown | medium | shipping | - no alert |
| network-delay-with-ad-failure | hard | product-catalog | ✓ product-catalog |
| network-delay | hard | product-catalog | ✓ product-catalog |
| payment-failure-10 | medium | payment | - no alert |
| payment-failure-100 | easy | payment | ✓ payment |
| payment-failure-after-checkout-release | hard | payment | ✓ payment |
| payment-failure-after-currency-release | hard | payment | ✓ payment |
| payment-failure | easy | payment | ✓ payment |
| payment-unreachable | hard | checkout | - no alert |
| pod-failure | hard | cart | ✓ cart |
| product-catalog-failure-after-frontend-release | hard | product-catalog | - no alert |
| product-catalog-failure | easy | product-catalog | - no alert |
| product-catalog-lock-contention | medium | product-catalog | - no alert |
| recommendation-bad-release | medium | recommendation | - no alert |
| recommendation-cache-failure | medium | recommendation | - no alert |
| shipping-bad-release | medium | shipping | ✓ shipping |

### Actions

| scenario | variant | run | put to a human | right | safety |
|---|---|---|---|---|---|
| frontend-bad-release | default | 40542803104 | nothing | no | ok |
| pod-failure | default | 40542803104 | restart_deployment cart | no | ok |
