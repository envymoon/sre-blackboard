version: 2

Your data: Prometheus (`prom_query` for one instant, `prom_range` for a trend). Establish
which services are unhealthy (error ratio, latency, traffic), since when, and which one
went bad first. Compare with a healthy period when it helps. Use client spans to see which
dependency a failing service is waiting on or getting errors from, and that dependency's own
server spans to see whether it is slow for everyone or only as seen from that caller.
