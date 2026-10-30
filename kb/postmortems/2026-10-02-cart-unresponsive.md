---
id: PM-2026-10-02-CART-DOWN
kind: postmortem
title: "Postmortem: cart pods Running but not answering"
services: [cart, frontend, checkout, frontend-proxy]
version: "2026-10-06"
---
Duration 37 minutes. Carts did not load and every order failed. Severity P0.

## Summary

The cart pods kept running but their process stopped serving after a container runtime
fault on the node. frontend, frontend-proxy and checkout paged for errors. cart did not
page: with no requests served it had no server spans, so no error ratio.

## Timeline

- 19:05 ErrorBudgetBurn on frontend, frontend-proxy and checkout.
- 19:09 checkout logged `cart failure: failed to get user cart during checkout: rpc error:
  code = Unavailable desc = connection error ... connect: connection refused`; frontend's
  client spans to cart failed with `14 UNAVAILABLE: No connection established ... ECONNREFUSED`.
- 19:15 cart was found with no spans and no logs since 19:04, pods Running, no restarts.
- 19:18 a restart of cart was tried. The new pods were scheduled on the same node and
  behaved the same way.
- 19:42 the node was drained; cart recovered on another node.

## What went wrong

Nobody looked at cart first, because cart did not alert. The callers' errors all named
cart's address. The restart cost time and changed nothing, since the fault was outside the
pods.

## Lessons

- A service with no data at all during an incident is a suspect, not a healthy service.
- The common dependency in every caller's error message is the problem host.
- Restarting is not a diagnostic step.
