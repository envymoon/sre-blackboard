---
id: RB-CHECKOUT
kind: runbook
title: checkout service
services: [checkout]
version: "2026-10-28"
---
checkout (Go, gRPC `oteldemo.CheckoutService/PlaceOrder`) places an order. It is the
busiest fan-out in the shop: one order calls cart, currency, product-catalog, shipping,
payment and email, then publishes to kafka. checkout pages for almost every dependency
failure on the order path, so it is the most common event host and a rare problem host.

## Dependencies and their addresses

checkout reads each dependency address from an environment variable: `CART_ADDR`,
`CURRENCY_ADDR`, `PAYMENT_ADDR`, `PRODUCT_CATALOG_ADDR`, `SHIPPING_ADDR`, `EMAIL_ADDR`, and
`KAFKA_ADDR`. The services listen on port 8080 inside the cluster. A release that changes
one of these variables breaks exactly one hop.

## Telling checkout's fault from a dependency's

Read checkout's error log or the error span's message. It names the step:
`failed to charge card` (payment), `cart failure` (cart), `failed to prepare order` (cart,
currency, product-catalog or shipping, see the rest of the message).

- The dependency's own server spans fail with the same message: the dependency is broken.
- The dependency has no server spans for those calls, and checkout's error is a dial error
  (`connection refused`, `i/o timeout`) on an unusual port: checkout's configuration is
  wrong. Look at checkout's latest revision.
- The dependency has no server spans and the error is `connection refused` on port 8080:
  the dependency's pods do not answer (RB-CHAOS).

## Safe actions

Rolling back checkout is right only for a checkout release that broke a hop. Restarting
checkout never fixes a dependency. checkout keeps no state between orders, so a restart is
harmless but useless for most incidents it pages for.
