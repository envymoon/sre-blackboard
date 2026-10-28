---
id: RB-ALERTS
kind: runbook
title: Alert reference - ErrorBudgetBurn and LatencyP95High
services: [all]
version: "2026-10-28"
---
The shop has two alert families, both computed from server spans per `service_name`.

## ErrorBudgetBurn

The SLO is 99% of server requests without error over 30 days (a 1% error budget).

- Fast burn: the 5-minute error ratio is above 14.4 times the budget (14.4%) for 1 minute.
  At that rate a 30-day budget lasts about two days. Severity critical, pages at once.
- Slow burn: the 30-minute error ratio is above 6 times the budget (6%) for 2 minutes.
  Severity warning.

Both need at least 10 requests in 5 minutes (the traffic guard), so a single failed
request on an idle service never pages. A service with low traffic can therefore fail
completely without alerting; check its callers.

The alert summary carries the ratio: "checkout is failing 47% of requests". A ratio near
50% on a payment path often means a percentage-based fault, not a random one.

## LatencyP95High

The 95th percentile of server span duration over 5 minutes is above 1000 ms for 2 minutes.
Latency alerts cascade up the call graph faster than error alerts: every caller waits for
its slowest dependency. frontend-proxy, frontend and checkout tend to page together when
one leaf service slows down.

## Labels and annotations

Each alert carries `service_name`, `severity` and `priority`. Alertmanager groups alerts by
alertname and service; one incident is opened per fingerprint. Resolved alerts close the
incident's alert, not the incident: a responder or the verifier closes it.

## Known noise

Feature flag names such as `productCatalogFailure`, `cartFailure` and
`recommendationCacheFailure` appear in many span attributes as flag evaluations, even when
the flag is off. A flag name in a span is not a lead; the flag's current variant is (check
the flag state). A small steady error ratio far below the thresholds is background, not
the incident: compare against the hour before.
