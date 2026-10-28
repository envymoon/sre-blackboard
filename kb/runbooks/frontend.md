---
id: RB-FRONTEND
kind: runbook
title: frontend and frontend-proxy
services: [frontend, frontend-proxy]
version: "2026-10-28"
---
frontend-proxy (Envoy) is the entry point: it routes to frontend (Next.js), image-provider
and the flagd UI. frontend renders the shop and calls ad, cart, checkout, currency,
product-catalog, recommendation and shipping.

## Why they page so often

Both sit at the top of the call graph, so any failing dependency on a page load makes them
fail. frontend-proxy's errors are frontend's errors seen one hop earlier. When frontend and
frontend-proxy page together with similar ratios, the fault is below frontend.

## Finding the dependency

Group frontend's failing client spans by span name. One dependency in all of them is the
lead: `CartService` for cart, `CurrencyService/Convert` for currency,
`ProductCatalogService` for product-catalog. Envoy's `upstream connect error` or
`no healthy upstream` from frontend-proxy means frontend itself is not answering.

## frontend's own faults

A frontend release (for example a catalog address on the wrong port) breaks one of
frontend's client calls right after the rollout, and frontend's dependency shows no server
spans for those calls. A release that only changes static content (a banner) does not
change any client call and does not cause errors.

## Actions

Rolling back frontend fixes frontend's own releases only. Restarting frontend-proxy is never
the fix for errors that come from below it.
