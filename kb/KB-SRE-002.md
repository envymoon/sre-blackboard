# KB-SRE-002 K8s Deployment release rollback and Pod startup failures

workload: Deployment/checkout-api RollingUpdate in two batches. Rollback is an R3
action requiring human approval; single-operator traffic shifts are forbidden.

## Release triage

On post-release 5xx, compare failing pods against pods still on the previous
revision. A clean control group on the old revision confirms the release as the
likely cause. Roll back to the last stable revision and verify the order success
rate, SLO burn, and timeout logs for 10 minutes.

## Pod startup failures

Check Pod events, container logs, image version, and the release ticket. Startup
code and config reads must be compared before blaming the scheduler or the node.
