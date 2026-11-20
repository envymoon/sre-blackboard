# Investigation: checkout error-rate SLO burn

**Alert:** checkout error ratio above its SLO burn threshold, 2026-10-15 ~03:33 UTC.
**Evidence:** `.evidence/` (5 entries), recorded in lab run 38672622429 of the sre-blackboard
fault lab and imported for this example; see each entry's `source`.

## Root cause

- [root-cause] checkout's rollout to revision `2` (change cause `checkout config release: payment client moved to port 9999`) set `PAYMENT_ADDR` to `payment:9999`; revision `1` had `payment:8080`. [E2]
- [root-cause] Checkout's calls to payment on that port time out: `PaymentService/Charge` spans fail with `dial tcp 10.96.201.161:9999: i/o timeout`, and `CheckoutService/PlaceOrder` fails with `failed to charge card`. [E4]

## Facts

- [fact] checkout's error ratio is `0.69`, an order of magnitude above every other service; frontend is at `0.039` and frontend-proxy at `0.041`. [E1]
- [fact] The new ReplicaSet `checkout-6f48f6dcc5` was created at `2026-10-15T03:30:06Z` and the old one, `checkout-7758999659`, was scaled `from 1 to 0`, about three minutes before the error ratio above was measured. [E2, E3]
- [fact] frontend logs `Checkout failed to place order` with the same `10.96.201.161:9999: i/o timeout`. [E5]

## Symptoms, not causes

- [inference] frontend and frontend-proxy fail because the orders they forward to checkout fail; their own error ratios (`3.9%`, `4.1%`) are a small share of all their traffic because only order placement goes through checkout. [E1, E5]

## Ruled out

- [ruled-out] payment's own error ratio is `0`: the failures are not raised inside payment. That is also what a payment service that never receives the calls would show, so this rules out a payment bug, not a payment outage. [E1]
- [ruled-out] The checkout rollout itself went through: events show the new pod `Started` and nothing at warning level. [E3]

## Hypotheses

- [hypothesis] payment listens on 8080 only, so nothing answers on 9999. Confirm with `kubectl get svc payment -o yaml` (not run in this example).

## Undo

Rolling checkout back to revision 1 restores `payment:8080`. This report proposes it; it does not do it.
