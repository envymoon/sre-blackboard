# KB-SRE-003 DB connection pool, slow queries, and cache hotspots

stock-db primary/replica plus redis-cache. Watch pool_usage, slow_queries,
lock_wait, replication_lag, and cache_hit_rate. DDL, DML, kill, and primary
failover are forbidden for agents.

## Pool saturation triage

When pool usage stays high with growing lock_wait, look for retry storms from a
recent release before scaling. Adding connections without fixing the retry policy
only moves the bottleneck. Never run DDL or kill queries as an automated action.

## Cache hotspots

A falling cache_hit_rate with a rising DB read rate points at a hotspot key.
Confirm with the key-access distribution, then throttle or split the hot key;
do not change eviction policy without approval.
