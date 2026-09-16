# KB-SRE-001 Checkout order API and P0 triage (SLO 99.9%/30d)

service_scope: checkout-service, POST /v1/checkout/orders, idempotent, 2s/3s timeouts.

## Order-create P0 triage

When INVENTORY_LOCK_TIMEOUT spikes, check the stock-db pool usage and slow queries
first (pool_usage, slow_queries, lock_wait), then the latest release change record.
Compare order-create 5xx against the payment-state baseline before concluding.

## Error codes

INVENTORY_LOCK_TIMEOUT means the inventory deduction call timed out waiting for a
stock-db connection. A rising timeout ratio with a healthy gateway points at the
checkout pods or the database pool, not at the gateway.
