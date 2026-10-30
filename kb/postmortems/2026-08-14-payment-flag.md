---
id: PM-2026-08-14-PAYMENT
kind: postmortem
title: "Postmortem: checkout paged for charges payment rejected"
services: [payment, checkout]
version: "2026-08-20"
---
Duration 41 minutes. About a quarter of orders failed. Severity P1.

## Summary

The `paymentFailure` flag was left on at 25% after a game day. payment rejected one in four
charges with `Payment request failed. Invalid token.` checkout failed every order whose
charge was rejected and paged for ErrorBudgetBurn; payment paged two minutes later.

## Timeline

- 10:02 flag changed to 25% during a game day; the game day ended without turning it off.
- 10:09 ErrorBudgetBurn on checkout (fast burn, 24%).
- 10:11 ErrorBudgetBurn on payment.
- 10:20 the responder rolled back checkout's latest revision. No change.
- 10:31 traces showed the error on payment's server spans, with the same message checkout
  logged after `could not charge the card`.
- 10:43 the flag owner turned the flag off; errors stopped within a minute.

## What went wrong

The responder treated checkout, the loudest alert, as the broken service, and rolled back a
healthy release. checkout's error text already named the payment call. payment's own
server spans failed at the same ratio as checkout's orders.

## Lessons

- Read the failing hop in checkout's error message before touching checkout.
- A flag change leaves no rollout history: check the flag state on every error incident.
- The share of failed requests matched a flag variant exactly (25%). Round percentages
  are a hint for a percentage flag.
- Rolling back checkout cost twelve minutes and changed nothing.
