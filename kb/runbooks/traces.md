---
id: RB-TRACES
kind: runbook
title: Reading traces in Jaeger
services: [all]
version: "2026-10-28"
---
Traces show the path of one request across services. Use them to confirm which hop fails
and to read the exact error message.

## Finding the failing hop

Search the alerting service for error traces in the alert window, then open one. Walk the
span tree from the root. The deepest span with an error status, whose children are
healthy or absent, is where the request broke. A parent marked as error because a child
failed is a symptom.

Error spans carry `exception.message` or `grpc.error_message`. Read it literally:

- `rpc error: code = Unavailable desc = connection error ... connect: connection refused`:
  nothing listens on that address. The target pods are gone, not ready, or the address is
  wrong.
- `dial tcp <ip>:<port>: i/o timeout`: packets go nowhere. Often a wrong port or a network
  policy; compare the port with the target service's ports.
- `deadline_exceeded: context deadline exceeded`: the callee answered too slowly.
- `14 UNAVAILABLE: No connection established. Last error: Error: connect ECONNREFUSED`: the
  Node.js gRPC client's form of connection refused.

## Missing spans are evidence

If checkout has a client span to payment but payment has no server span for it, the request
never reached payment. That clears payment and points at the caller's configuration or the
network between them.

## Sampling and volume

The demo samples every trace, but the tool returns a bounded number of traces per query.
Narrow by service, operation and a short window rather than asking for more traces.
