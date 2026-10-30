---
id: RB-PRODUCT-CATALOG
kind: runbook
title: product-catalog service and its database
services: [product-catalog, astronomy-db]
version: "2026-10-28"
---
product-catalog (Go, gRPC `oteldemo.ProductCatalogService/GetProduct`, `ListProducts`,
`SearchProducts`) serves the product list from its PostgreSQL database, which appears as
`astronomy-db` in its client spans. frontend, recommendation and checkout call it.

## Typical failures

- `productCatalogFailure` flag: GetProduct fails for one product id only. A small, steady
  error ratio on product-catalog and frontend; the error names the product.
- Lock contention in the database: product-catalog's client spans to astronomy-db slow down
  and only those; its calls to flagd stay fast. product-catalog's latency rises.
- A slow network to product-catalog: every client span of product-catalog slows by the same
  amount (astronomy-db and flagd alike), and so do its server spans. Its callers
  (recommendation, frontend, checkout) slow down and time out with `deadline_exceeded`.

## Database or network?

This is the most common misdiagnosis on this service. product-catalog's own incident sees
slow database spans and blames astronomy-db. Before blaming the database, compare the
latency of product-catalog's calls to flagd: a database problem leaves them alone, a
network problem slows them too. Also check the database's own load; the demo database is
small and rarely the bottleneck.

## Actions

Do not run DDL, kill queries or fail over the database as an automated action. A rollback
of product-catalog helps only after a product-catalog release.
