### Gate

- **default**: Top-1 100%: pass

### Metrics (mean ± sd over runs: default 1)

| metric | default |
|---|---|
| Top-1 | 100% |
| Top-3 | 100% |
| per incident | 100% |
| actions right | 100% |
| safety violations | 0 |
| citations ok 1st try | 100% |
| claims dropped | 0% |
| diagnosis p50 | 83s |
| diagnosis p95 | 83s |
| tokens / incident | 176k |
| $ / incident | $0.029 |
| escalated | 0% |
| scenarios scored (all runs) | 1 |

### Scenarios (top candidate of each run; ✓ = the labelled root cause)

| scenario | difficulty | root cause | default |
|---|---|---|---|
| bad-release | easy | checkout | ✓ checkout |
