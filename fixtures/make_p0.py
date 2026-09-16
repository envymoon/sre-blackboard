"""Generates the P0 baseline-input fixture. Maps to spec Sec.2.4, alert_id alt-ck-prod-20260915-100312-001."""
import json, pathlib
fixture = {
  "alert_id": "alt-ck-prod-20260915-100312-001",
  "tenant_id": "tenant-checkout",
  "environment": "production",
  "priority": "P0",
  "alert_name": "CheckoutCreate5xxRateHigh",
  "service_id": "checkout-service",
  "workload": "Deployment/checkout-api",
  "api": "POST /api/v1/checkout/orders",
  "slo": "99.9%/30d",
  "problem_id": "prb-ck-order-create-20260915-001",
  "error_rate": "42.6%",
  "problem_hosts": ["checkout-pod-7f8c-a", "checkout-pod-7f8c-b"],
  "event_hosts": ["prod-gw-03"],
  "change_id": "chg-20260915-884",
  "known_events": [{"type": "deployment", "at": "09:56:40", "change_id": "chg-20260915-884"}]
}
out = pathlib.Path(__file__).with_name("p0_baseline.json")
out.write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
print("wrote", out)
