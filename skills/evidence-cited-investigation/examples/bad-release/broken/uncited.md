# Broken on purpose: a fact with nothing behind it

- [fact] payment's pods are healthy and passing readiness checks.
- [root-cause] checkout's rollout to revision `2` set `PAYMENT_ADDR` to `payment:9999`. [E2]
