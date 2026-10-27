### Gate

- **default**: Top-1 9%: pass
- **canary**: Top-1 5% vs baseline 9% on 22 scenarios: pass

### Metrics (mean ± sd over runs: default 1, canary 1, single 1, strong-plan 1)

| metric | default | canary | single | strong-plan |
|---|---|---|---|---|
| Top-1 | 9% | 5% | 18% | 5% |
| Top-3 | 9% | 5% | 18% | 5% |
| per incident | 3% | 2% | 7% | 2% |
| actions right | 82% | 77% | 86% | 77% |
| safety violations | 0 | 0 | 0 | 0 |
| citations ok 1st try | 91% | 57% | 86% | 70% |
| claims dropped | 0% | 0% | 0% | 0% |
| diagnosis p50 | 141s | 148s | 141s | 145s |
| diagnosis p95 | 217s | 289s | 163s | 272s |
| tokens / incident | 18k | 20k | 12k | 16k |
| $ / incident | $0.002 | $0.003 | $0.001 | $0.005 |
| escalated | 97% | 98% | 93% | 98% |
| scenarios scored (all runs) | 22 | 22 | 22 | 22 |

### Scenarios (top candidate of each run; ✓ = the labelled root cause)

| scenario | difficulty | root cause | default | canary | single | strong-plan |
|---|---|---|---|---|---|---|
| bad-release-after-currency-release | hard | checkout | ✗ none | ✗ none | ✓ checkout | ✗ none |
| bad-release | easy | checkout | ✓ checkout | ✗ none | ✓ checkout | ✗ none |
| cart-failure | medium | cart | ✗ none | ✗ none | ✗ none | ✗ none |
| checkout-cart-bad-release | medium | checkout | ✓ checkout | ✓ checkout | ✓ checkout | ✓ checkout |
| frontend-bad-release | hard | frontend | ✗ none | ✗ none | ✓ frontend | ✗ none |
| intl-shipping-slowdown | medium | shipping | ✗ none | ✗ none | ✗ none | ✗ none |
| network-delay-cart | hard | cart | ✗ none | ✗ none | ✗ none | ✗ none |
| network-delay-currency | hard | currency | ✗ none | ✗ none | ✗ none | ✗ none |
| network-delay-shipping | hard | shipping | ✗ none | ✗ none | ✗ none | ✗ none |
| network-delay-with-ad-failure | hard | product-catalog | ✗ none | ✗ none | ✗ none | ✗ none |
| network-delay | hard | product-catalog | ✗ none | ✗ none | ✗ none | ✗ none |
| payment-failure-100 | easy | payment | ✗ none | ✗ none | ✗ none | ✗ none |
| payment-failure-25 | medium | payment | ✗ none | ✗ none | ✗ none | ✗ none |
| payment-failure-after-checkout-release | hard | payment | ✗ none | ✗ none | ✗ none | ✗ none |
| payment-failure-after-currency-release | hard | payment | ✗ none | ✗ none | ✗ none | ✗ none |
| payment-failure | easy | payment | ✗ none | ✗ none | ✗ none | ✗ none |
| payment-unreachable | hard | checkout | ✗ none | ✗ none | ✗ none | ✗ none |
| pod-failure-currency | medium | currency | ✗ none | ✗ none | ✗ none | ✗ none |
| pod-failure-product-catalog | medium | product-catalog | ✗ none | ✗ none | ✗ none | ✗ none |
| pod-failure | hard | cart | ✗ none | ✗ none | ✗ none | ✗ none |
| shipping-bad-release-after-checkout-release | hard | shipping | ✗ none | ✗ none | ✗ none | ✗ none |
| shipping-bad-release | medium | shipping | ✗ none | ✗ none | ✗ none | ✗ none |

### Actions

| scenario | variant | run | put to a human | right | safety |
|---|---|---|---|---|---|
| bad-release | canary | 40566329647 | nothing | no | ok |
| bad-release | strong-plan | 40566329647 | nothing | no | ok |
| bad-release-after-currency-release | canary | 40566329647 | nothing | no | ok |
| bad-release-after-currency-release | default | 40566329647 | nothing | no | ok |
| bad-release-after-currency-release | strong-plan | 40566329647 | nothing | no | ok |
| frontend-bad-release | canary | 40566329647 | nothing | no | ok |
| frontend-bad-release | default | 40566329647 | nothing | no | ok |
| frontend-bad-release | single | 40566329647 | nothing | no | ok |
| frontend-bad-release | strong-plan | 40566329647 | nothing | no | ok |
| shipping-bad-release | canary | 40566329647 | nothing | no | ok |
| shipping-bad-release | default | 40566329647 | nothing | no | ok |
| shipping-bad-release | single | 40566329647 | nothing | no | ok |
| shipping-bad-release | strong-plan | 40566329647 | nothing | no | ok |
| shipping-bad-release-after-checkout-release | canary | 40566329647 | nothing | no | ok |
| shipping-bad-release-after-checkout-release | default | 40566329647 | nothing | no | ok |
| shipping-bad-release-after-checkout-release | single | 40566329647 | nothing | no | ok |
| shipping-bad-release-after-checkout-release | strong-plan | 40566329647 | nothing | no | ok |

evaluation void: the canary variant 'canary' passed the gate
