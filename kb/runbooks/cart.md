---
id: RB-CART
kind: runbook
title: cart service and valkey-cart
services: [cart, valkey-cart]
version: "2026-10-28"
---
cart (.NET, gRPC `oteldemo.CartService/GetCart`, `AddItem`, `EmptyCart`) stores carts in
valkey-cart, a Valkey (Redis compatible) instance. frontend calls cart on every page that
shows a cart; checkout calls GetCart and EmptyCart for every order.

## Typical failures

- `cartFailure` flag: cart fails that share of EmptyCart calls with a store error. cart's
  server spans carry the error; checkout fails the affected orders.
- cart pods stop answering: frontend and checkout fail with `connect ECONNREFUSED` or
  `connection refused` to cart's address on port 8080, and checkout logs `cart failure:
  failed to get user cart during checkout`. cart itself shows no server spans and no logs,
  so its own error ratio does not rise. frontend, frontend-proxy and checkout page.
- valkey-cart is down or slow: cart's server spans fail or slow down, and cart logs Redis
  connection errors. cart is the event host, valkey-cart the problem host.
- cart not ready after a restart: its readiness probe fails and Kubernetes events show
  `Readiness probe failed`; requests go to the remaining ready pods, if any.

## Actions

A restart of cart (`restart_deployment`) helps only if the pods are wedged by their own state. If the
pods are Running, healthy in Kubernetes terms and still do not answer, the fault comes from
outside the pod and a restart does not help (RB-CHAOS). Carts live in valkey-cart, so
restarting cart loses no carts; restarting valkey-cart does.
