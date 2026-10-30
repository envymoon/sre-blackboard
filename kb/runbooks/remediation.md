---
id: RB-REMEDIATION
kind: runbook
title: Remediation actions and when to use them
services: [all]
version: "2026-10-28"
---
The automated system can propose only catalogue actions, each with a risk tier, and a human
approves any change. Choose the action that addresses the cause; an action on the event
host is wrong when the problem host is elsewhere.

## The catalogue

- `rollout_undo` (deployment): back to the previous revision. For a release that broke the
  service. Requires a diagnosis that names the revision and its effect.
- `restart_deployment` (deployment): new pods with the same spec. For pods wedged by their own
  state (a leak, a stuck connection pool). Useless for flags, for configuration errors and
  for faults applied to the pods from outside.
- `scale_deployment` (deployment, replicas within limits): for saturation with a clear capacity cause.

## Do not

- Do not roll back a service whose data is healthy because it released recently.
- Do not restart a service to "see if it helps": it destroys evidence and changes nothing
  for flag-driven or external faults.
- Do not act on the database: no DDL, no killed queries, no failover.
- Do not change feature flags; name the flag and its variant for its owner.

## When there is no action

Many incidents have no catalogue fix: a failure flag, a network fault, an unresponsive pod
that is not wedged by its own state. Then the right plan has no action, a clear diagnosis
and an escalation to the owner. An empty plan is a valid answer.

## After an action

Verify on the alerting service's error ratio and latency for five to ten minutes. No
improvement means the diagnosis was wrong: say so in the incident.
