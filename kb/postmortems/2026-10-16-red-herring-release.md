---
id: PM-2026-10-16-RED-HERRING
kind: postmortem
title: "Postmortem: a currency release blamed for payment failures"
services: [payment, checkout, currency]
version: "2026-10-20"
---
Duration 49 minutes. Half of all orders failed. Severity P1.

## Summary

currency rolled out a routine release ("refresh exchange rates hourly") at 16:01. Two
minutes later the `paymentFailure` flag went to 50% by mistake. checkout failed every other
order and paged. The responder saw the currency release in checkout's dependency list at
the right time and rolled currency back. Nothing changed; the flag was found 30 minutes later.

## Timeline

- 16:01 currency revision 4 rolled out.
- 16:03 `paymentFailure` set to 50%.
- 16:06 ErrorBudgetBurn on checkout (fast burn, 49%), then payment.
- 16:12 currency rolled back. No change in errors.
- 16:35 payment's server spans failed with `Payment request failed. Invalid token.`; the
  flag state showed `paymentFailure` at 50%.
- 16:55 flag turned off; recovery within a minute.

## What went wrong

The release was close in time and on the order path, so it looked like a cause. But
currency's error ratio was zero throughout and checkout's errors were all on the payment
call (`failed to charge card`), none on currency.

## Lessons

- A release is a suspect until the changed service's data clears it; currency's clean data
  should have cleared it in minutes.
- Check what the failing calls have in common before checking what changed.
- Rolling back a healthy service is a change made for nothing, with its own risk.
