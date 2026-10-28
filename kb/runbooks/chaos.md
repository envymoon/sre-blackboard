---
id: RB-CHAOS
kind: runbook
title: Network delay and unresponsive pods
services: [all]
version: "2026-10-28"
---
Two failure patterns do not show up as a release or a flag: the network to one service
gets slow, or a service's pods stop answering while Kubernetes still reports them Running.

## A slow network to one service

Symptoms: one service's server latency rises, and every one of its client spans rises by
about the same amount, whatever the dependency: its database, flagd, other services. Its
callers then slow down and may hit `deadline_exceeded: context deadline exceeded`.

The trap: the slow service's own spans to its database look slow, so its incident blames
the database. A database problem slows the database calls only; a network problem on the
service slows all of its calls equally. Compare the client latency to flagd with the one
to the database: if both rose by the same amount, the database is a victim.

## Pods that are Running but do not answer

Symptoms: callers fail with `connection refused` or `UNAVAILABLE: No connection
established`; the target has no server spans and no logs at all; its pods are Running, not
restarting, with no recent events. The pods' containers may have been replaced or frozen.

Because the broken service records nothing, its own error ratio is not elevated: it has no
requests at all. Read the callers' client spans to find it. The service whose name is in
every caller's error message is the problem host.

## What does not help

A restart creates new pods with the same fault when the fault is applied to the pods from
outside; it also loses state for whoever investigates next. Scaling up adds more pods that
behave the same way. Report the service and the pattern, and escalate to the platform team.
