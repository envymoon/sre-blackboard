---
id: RB-CURRENCY
kind: runbook
title: currency service
services: [currency]
version: "2026-10-28"
---
currency (C++, gRPC `oteldemo.CurrencyService/Convert`, `GetSupportedCurrencies`) converts
prices. frontend calls it to render every price; checkout calls it to convert an order to
the user's currency. It is fast, stateless and rarely changes, which makes it a common red
herring when it does get a release.

## Typical failures

- currency pods do not answer: frontend's and checkout's client spans to currency fail with
  `connection refused`, checkout logs `failed to prepare order: failed to convert price of
  "<product id>" to USD`. currency has no server spans to fail, so the errors are all on its
  callers; currency's own error ratio stays at zero or has no data. frontend and
  frontend-proxy page first, checkout soon after.
- A currency release: currency rolls out a new revision (for example an exchange rate
  refresh setting). Unless currency's server spans fail or its callers fail on calls to it,
  the release is unrelated to whatever else is failing at the time.

## Ruling currency out

Query currency's server error ratio and its callers' client spans to `CurrencyService`.
Both clean: currency is not involved, even if it rolled out minutes before the alert.

## Actions

Rolling back a currency release is right only when currency's own data shows the
failure. When currency's pods are Running but do not answer, a restart does not help.
