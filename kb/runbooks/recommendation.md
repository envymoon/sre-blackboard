---
id: RB-RECOMMENDATION
kind: runbook
title: recommendation and ad services
services: [recommendation, ad]
version: "2026-10-28"
---
recommendation (Python) suggests products and calls product-catalog for their details. ad
(Java) serves ads. frontend calls both on product pages; neither is on the order path, so
their failures degrade pages but do not fail orders.

## recommendation

- `recommendationCacheFailure` flag: recommendation's cache grows without bound and its
  latency climbs over minutes; memory usage rises with it.
- product-catalog is slow or failing: recommendation's client spans to product-catalog fail
  or time out with `deadline_exceeded`. recommendation is an event host here.
- A recommendation release with a wrong `PRODUCT_CATALOG_ADDR`: its client spans to
  product-catalog fail with a dial error and product-catalog sees no calls from it.

## ad

- `adFailure` flag: ad fails GetAds requests.
- `adHighCpu`: ad burns CPU and slows down; `adManualGc`: ad pauses for full garbage
  collections, a sawtooth latency pattern.
- ad errors make frontend's product pages fail a call, and frontend may page. An ad
  failure at the same time as another incident is usually unrelated: check whether the
  other incident's errors involve ad at all.

## Actions

None of the flags is fixed by a restart for long: the behaviour returns while the flag is on.
