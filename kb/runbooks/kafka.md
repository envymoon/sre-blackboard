---
id: RB-KAFKA
kind: runbook
title: kafka, accounting and fraud-detection
services: [kafka, accounting, fraud-detection, email]
version: "2026-10-28"
---
After an order is placed, checkout publishes it to the kafka topic `orders`. accounting
(.NET) and fraud-detection (Kotlin) consume it. email (Ruby) sends the confirmation, called
by checkout over HTTP.

## Off the request path

kafka consumers run after the order is confirmed. A slow or failing consumer does not
fail an order and does not raise checkout's error ratio; it shows up as consumer lag and as
the consumer's own spans. If checkout is failing, the consumers are almost never the cause.

checkout's publish to kafka can fail if kafka itself is down; checkout then logs a kafka
producer error, and the order may still succeed depending on the version.

## email

- `emailMemoryLeak` flag: email's memory grows until it is OOM killed; Kubernetes events
  show the container restarting. checkout's calls to email fail during the restarts.

## What to check

For an incident on these services: consumer spans and their errors, container restarts and
OOM kills in the events, and memory over the last hour.
