---
id: RB-FLAGS
kind: runbook
title: Feature flags in flagd
services: [flagd, payment, cart, product-catalog, recommendation, ad, checkout]
version: "2026-10-28"
---
Every demo service reads feature flags from flagd. Several flags exist to inject failures;
a flag change is a change like a release, but it leaves no rollout history.

## Failure flags

- `paymentFailure` (off, 10%, 25%, 50%, 75%, 90%, 100%): payment rejects that share of
  charges with `Payment request failed. Invalid token.` The error is in payment's own code:
  payment's server spans fail, and checkout fails only the orders whose charge failed.
- `paymentUnreachable` (on/off): checkout dials a payment address that does not exist.
  payment is healthy and sees no traffic; the fault is in checkout's configuration.
- `cartFailure` (percentages): cart fails that share of calls (a bad store).
- `productCatalogFailure` (on/off): product-catalog fails GetProduct for one product.
- `recommendationCacheFailure`: recommendation's cache leaks and slows down.
- `adFailure`, `adHighCpu`, `adManualGc`: the ad service fails, burns CPU or pauses for GC.
- `emailMemoryLeak`: email leaks memory.

## Reading the flag state

The flag state lists each flag's `defaultVariant`. Anything other than `off` on a failure
flag during an incident is a strong lead, but it is still a lead: confirm with the service's
spans that the failure matches what the flag does, and with the alert time.

## Turning a flag off

No automated action changes flags. A flag is turned off by its owner in the flagd
configuration; the diagnosis should name the flag and its variant so that a human can.
Restarting or rolling back the affected service does nothing while the flag stays on.
