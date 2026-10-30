---
id: RB-PAYMENT
kind: runbook
title: payment service
services: [payment]
version: "2026-10-28"
---
payment (Node.js, gRPC `oteldemo.PaymentService/Charge`) validates a card and charges it.
Its only caller is checkout. It keeps no state and calls nothing but flagd.

## Typical failures

- Charges rejected with `Payment request failed. Invalid token.` and a loyalty level
  attribute (`demo.user_context.loyalty_level=gold`): the `paymentFailure` flag is on. The
  share of failed charges equals the flag's variant (10%, 25%, 50%, 100%). payment's own
  server spans carry the error, and payment's error ratio is about the variant.
- checkout fails with `could not charge the card: rpc error: code = Unavailable` and a dial
  error, and payment shows no server spans: payment never got the calls. Either checkout
  dials the wrong address (a checkout release, or the `paymentUnreachable` flag) or payment
  pods do not answer.

## How it shows up

checkout pages first and loudest, because every failed charge fails an order: checkout's
error ratio follows payment's. payment pages too when it has enough traffic. With both
alerts open, payment is the problem host when its server spans fail with the same message.

## Actions

No restart or rollback fixes a flag. If payment has no recent revision, do not roll it
back. The fix for `paymentFailure` is turning the flag off, by its owner.
