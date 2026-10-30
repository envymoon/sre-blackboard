---
id: PM-2026-10-09-CURRENCY-DOWN
kind: postmortem
title: "Postmortem: currency unavailable, errors only on its callers"
services: [currency, frontend, frontend-proxy, checkout]
version: "2026-10-12"
---
Duration 22 minutes. Prices did not render and orders failed. Severity P1.

## Summary

The currency pods stopped answering. frontend could not render prices and paged with
frontend-proxy; checkout failed orders with `failed to prepare order: failed to convert
price of "<product>" to USD`. currency's own error ratio stayed empty: it served nothing.

## Timeline

- 08:40 ErrorBudgetBurn on frontend and frontend-proxy.
- 08:43 ErrorBudgetBurn on checkout.
- 08:47 frontend was suspected; its last release was two days old.
- 08:52 the failing client spans of frontend and checkout all targeted
  `oteldemo.CurrencyService/Convert` with `connection refused`.
- 09:02 currency recovered after its node was replaced.

## What went wrong

The dashboard of server error ratios showed currency as healthy (no data), and the first
suspect was the loudest alert. The callers' client spans named currency within minutes.

## Lessons

- Server metrics cannot show a service that receives nothing. Use client spans.
- When frontend and checkout fail on the same dependency, the dependency is the lead.
