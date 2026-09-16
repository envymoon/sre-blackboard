# KB-SRE-004 Edge gateway 502 and downstream timeout propagation

gateway -> checkout-service -> inventory-service -> stock-db. The gateway is an
observation point only; traffic shifts and route changes are forbidden for agents.

## 502 triage

A gateway 502 with healthy gateway CPU and memory means the failure originates
downstream. Follow the trace path hop by hop: gateway, checkout-service,
inventory deduction, stock-db. The first hop with errors plus a matching change
record is the investigation lead.

## Timeout propagation

Fixed-interval retries without a total-attempt cap turn one slow dependency into
a pool-wide outage. Check the retry policy of the failing client before blaming
the database.
