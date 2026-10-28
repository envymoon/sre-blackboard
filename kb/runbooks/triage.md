---
id: RB-TRIAGE
kind: runbook
title: First response to an SLO alert
services: [all]
version: "2026-10-28"
---
Use this for every ErrorBudgetBurn or LatencyP95High page, before any service runbook.
The goal of the first ten minutes is to find the problem host, not to fix the event host.

## Event host and problem host

The alert fires on the service where users see the failure: the event host. The fault is
often one or two hops further down the call graph: the problem host. checkout pages when
payment rejects charges; frontend pages when cart stops answering. Treat the alerting
service as a witness until its own data clears or convicts it.

Rule of thumb: if the alerting service's server spans fail, but its client spans to one
dependency fail at the same rate and with the same message, follow that dependency.

## First checks

1. List every open incident in the environment. Several services paging at once usually
   share one cause; the deepest service in the call graph that is failing is the lead.
2. Query `service:sli_error_ratio:rate5m` for all services, not only the alerting one.
3. Compare error start times. The service that went bad first is the better suspect.
4. Read the error messages on the failing spans. A message names the hop that broke:
   `connection refused`, `i/o timeout`, `deadline exceeded`, `Invalid token`.
5. Check changes in the 30 minutes before the alert: rollouts, flag changes, events.

## Ruling things out

Write down what you checked and found normal. "currency error ratio 0, no rollout in two
hours" is a finding: it removes a suspect and stops the next responder from checking it
again. A change that happened near the alert is a suspect, not a cause: it needs data on
the changed service that shows the failure.

## When to escalate

Escalate to a human when the evidence points at two unrelated services, when the problem
host is outside the namespace (a node, the cluster network, the cloud provider), or when
no data source answers. Guessing a root cause costs more than a page.
