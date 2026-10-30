---
id: PM-2026-09-21-CATALOG-LATENCY
kind: postmortem
title: "Postmortem: product-catalog slowed by its network, database blamed"
services: [product-catalog, astronomy-db, recommendation, frontend, checkout]
version: "2026-09-25"
---
Duration 58 minutes. Product pages and checkout slow, some orders timed out. Severity P1.

## Summary

A node network problem added about 800 ms to every packet to and from the product-catalog
pod. product-catalog's latency rose, recommendation, frontend, frontend-proxy and checkout
followed with LatencyP95High, and recommendation's calls timed out with `deadline_exceeded`.

## Timeline

- 03:14 LatencyP95High on product-catalog, then recommendation, frontend, checkout.
- 03:22 product-catalog's client spans to astronomy-db were slow; the database was blamed.
- 03:40 the database team found no load, no locks and normal query times on the server side.
- 03:51 product-catalog's client spans to flagd were slow by the same amount as those to the
  database: the slowness was on product-catalog's side of every connection.
- 04:12 the pod was moved to another node; latency recovered.

## What went wrong

The investigation took the slow database spans at face value. Every outbound call of
product-catalog, whatever the target, was slower by the same amount. A database problem
cannot slow product-catalog's calls to flagd.

## Lessons

- Compare a service's client latency across all its dependencies before blaming one.
- The callers' alerts were symptoms; the deepest slow service was product-catalog, and the
  cause was its network, not its database.
- No catalogue action fixes a network fault. Escalate to the platform team early.
