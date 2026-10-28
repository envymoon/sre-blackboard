---
id: PM-2026-09-03-CHECKOUT-PORT
kind: postmortem
title: "Postmortem: a checkout config release dialled payment on the wrong port"
services: [checkout, payment]
version: "2026-09-08"
---
Duration 26 minutes. Every order failed. Severity P0.

## Summary

A checkout release changed `PAYMENT_ADDR` to `payment:8081` while payment listens on 8080.
Every charge failed with `could not charge the card: rpc error: code = Unavailable desc =
connection error ... dial tcp <ip>:8081: i/o timeout`. checkout paged at 100% errors.
payment was healthy the whole time.

## Timeline

- 14:30 checkout revision 7 rolled out, change-cause "checkout config: payment client port".
- 14:32 ErrorBudgetBurn on checkout (fast burn, 100%).
- 14:36 payment was suspected first because the errors mention the payment call.
- 14:44 payment had no server spans for the failed calls: the requests never arrived.
- 14:47 the diff of checkout revision 7 against 6 showed the new `PAYMENT_ADDR`.
- 14:52 checkout rolled back (rollout_undo); orders recovered within two minutes.

## What went wrong

The error text names payment, but the error is a dial error on an unusual port, and payment
saw nothing. The release on checkout two minutes before the alert explained the failure
exactly.

## Lessons

- A dial error (`i/o timeout`, `connection refused`) plus no server spans on the callee
  means the caller cannot reach it; check the caller's configuration.
- Read the deployment diff of the alerting service when it rolled out shortly before.
- Rolling back checkout was the right action here because checkout's release broke it,
  confirmed by the diff and by the recovery.
