"""Store contract: MySQL is the source of truth, Redis holds hot state only.
Maps to spec Sec.16. Local default is in-memory; table/key names match the spec so the
implementation cannot drift. Production uses external MySQL."""
from __future__ import annotations

# Sec.16.1 MySQL core tables (production uses external MySQL; names pinned here)
MYSQL_TABLES = ["incidents", "tasks", "artifacts", "checkpoints", "traces", "approvals", "eval_freeze"]

# Sec.16.2 Redis keys, TTLs, and degrade rules (production uses external Redis)
REDIS_KEYS = {
    "queue:incident": "ZSET priority+ts",
    "lease:task:{task_id}": "TTL 90s; returns to READY when heartbeat stops",
    "knowledge:cache:{tenant}:{query_fp}:{kb_version}": "TTL 900s, top 5 entries",
}
