---
id: RB-RELEASE
kind: runbook
title: Investigating a suspected bad release
services: [all]
version: "2026-10-28"
---
Every service is a Deployment of the same name in namespace `otel-demo`. A release is a
new ReplicaSet revision; its reason is the `kubernetes.io/change-cause` annotation.

## Is a release involved?

1. Read the rollout history of the failing service and of every dependency on the failing
   path. Note the time of each revision against the alert start.
2. For a revision shortly before the alert, read the diff against the previous revision:
   image, environment variables, arguments, ports, resources.
3. A release is the cause only when the changed setting explains the error. An environment
   variable that moves a client address (for example a payment address on port 9999 where
   payment listens on 8080) explains `connection refused` or `i/o timeout` on that hop.

## Releases that are not the cause

Releases happen all the time. A rollout of a service whose own error ratio stays at zero,
and whose callers do not fail on calls to it, did not cause the incident, however close in
time it is. Rolling back a healthy service is a change made for nothing, and it hides the
real cause for a while because responders wait to see whether it helped.

Compare the failure with the change: does the failing hop involve the changed service? Do
the errors start at the rollout, or before it, or minutes after?

## Rolling back

`rollout_undo` on the deployment returns to the previous revision. It needs approval and a
diagnosis that names the release, with evidence from the changed service. Verify afterwards:
the error ratio of the alerting service should fall within five minutes. If it does not,
the release was not the cause; say so and keep investigating.

## Config-only releases

A change of environment variables only, with the same image, is still a release. The demo
services read their dependency addresses from variables such as `PAYMENT_ADDR`,
`CART_ADDR`, `CURRENCY_ADDR`; a wrong value there breaks one hop and leaves the rest working.
