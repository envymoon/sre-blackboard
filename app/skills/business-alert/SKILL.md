# business-alert SKILL (spec Sec.13.3): user impact and recovery verification. Never touches orders/charges/refunds/makeup-orders.
# Script chain: load_business_baseline -> check_funnel -> verify_recovery (recovered only after two consecutive business cycles meet criteria)
