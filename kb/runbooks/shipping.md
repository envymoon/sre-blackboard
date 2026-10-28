---
id: RB-SHIPPING
kind: runbook
title: shipping and quote services
services: [shipping, quote]
version: "2026-10-28"
---
shipping (Rust, HTTP and gRPC `GetQuote`, `ShipOrder`) computes shipping costs by calling
quote (PHP, HTTP `/getquote`). frontend calls shipping for the cart page; checkout calls it
for every order.

## Typical failures

- quote is slow: shipping's client spans to quote slow down, shipping's latency rises, and
  checkout and frontend follow. International orders take longer than domestic ones when
  quote computes them differently, so a slowdown can affect only some orders.
- shipping dials the wrong quote address (a shipping release that changes `QUOTE_ADDR`):
  shipping's client spans to quote fail with a connection error, quote sees no requests.
  shipping is the problem host even though the error mentions quote.
- shipping itself fails: its server spans carry the error and quote is quiet.

## Reading checkout's view

checkout logs `failed to get shipping quote` or fails in `prepare order` when shipping
fails. Follow it to shipping, then read shipping's client spans to quote.

## Actions

Roll back shipping only after a shipping release that explains the error. quote has no
state; a restart of quote helps only when quote's own pods are unhealthy.
