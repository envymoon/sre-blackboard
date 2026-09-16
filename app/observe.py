"""Observability. Maps to spec Sec.20.2/20.3: performance targets + platform self-metrics.
Targets are calibrated after load tests; only the metric definitions are pinned here."""
from __future__ import annotations

PERF_TARGETS = {"ack_s": 1, "normalize_dedup_s": 5, "first_summary_p0_s": 120,
                "first_summary_p1_s": 300, "readonly_success": 0.99,
                "double_claim": 0, "trace_complete": 1.0}

# Sec.20.3 platform self-monitoring: intake / dedup / ack latency / tasks / tools / tokens / safety / remediation / adoption
OBSERVE_KEYS = ["alerts", "dedup_rate", "ack_latency", "tasks", "retries", "timeouts", "dlq",
                "tool_latency", "tool_errors", "perm_denies", "tokens", "cost",
                "safety_denies", "approval_wait", "auto_success", "rollbacks", "adoptions"]


class Monitor:
    def __init__(self):
        self.counters: dict = {k: 0 for k in OBSERVE_KEYS}

    def incr(self, key: str, n: int = 1):
        self.counters[key] = self.counters.get(key, 0) + n

    def snapshot(self) -> dict:
        return {"targets": PERF_TARGETS, "counters": dict(self.counters)}
