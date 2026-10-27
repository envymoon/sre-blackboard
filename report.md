### Gate

- **default**: Top-1 75%: pass

### Metrics (mean ± sd over runs: default 1)

| metric | default |
|---|---|
| Top-1 | 75% |
| Top-3 | 75% |
| per incident | 75% |
| actions right | 100% |
| safety violations | 0 |
| citations ok 1st try | 95% |
| claims dropped | 0% |
| diagnosis p50 | 130s |
| diagnosis p95 | 162s |
| tokens / incident | 303k |
| $ / incident | $0.044 |
| escalated | 0% |
| scenarios scored (all runs) | 4 |

### Scenarios (top candidate of each run; ✓ = the labelled root cause)

| scenario | difficulty | root cause | default |
|---|---|---|---|
| ad-failure | easy | ad | - no alert |
| ad-high-cpu | medium | ad | - no alert |
| cart-failure | medium | cart | ✓ cart |
| cart-readiness-failure | hard | cart | - no alert |
| cpu-stress | medium | recommendation | - no alert |
| email-memory-leak | medium | email | - no alert |
| intl-shipping-slowdown | medium | shipping | ✓ shipping |
| payment-failure-10 | medium | payment | - no alert |
| payment-unreachable | hard | checkout | ✓ checkout |
| product-catalog-failure-after-frontend-release | hard | product-catalog | ✗ cart |
| product-catalog-failure | easy | product-catalog | - no alert |
| product-catalog-lock-contention | medium | product-catalog | - no alert |
| recommendation-bad-release | medium | recommendation | - no alert |
| recommendation-cache-failure | medium | recommendation | - no alert |
