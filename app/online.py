"""Online evaluation + observability. Maps to spec Sec.21.2/Sec.20: MTTA/MTTR/noise
reduction/takeover/adoption/cost/P0 misses; P0 SLAs are 30s to start, 90s to first evidence."""
from __future__ import annotations

P0_START_SLA_S = 30
P0_EVIDENCE_SLA_S = 90


class OnlineMetrics:
    def __init__(self):
        self.incidents: list = []

    def record(self, mtta_s: float, mttr_s: float, adopted: bool, auto_ok: bool, cost_usd: float, p0_missed: bool = False):
        self.incidents.append({"mtta": mtta_s, "mttr": mttr_s, "adopted": adopted,
                               "auto_ok": auto_ok, "cost": cost_usd, "p0_missed": p0_missed})

    def summary(self) -> dict:
        n = max(1, len(self.incidents))
        return {"mtta_avg": sum(i["mtta"] for i in self.incidents) / n,
                "mttr_avg": sum(i["mttr"] for i in self.incidents) / n,
                "adoption": sum(i["adopted"] for i in self.incidents) / n,
                "auto_success": sum(i["auto_ok"] for i in self.incidents) / n,
                "cost_total": round(sum(i["cost"] for i in self.incidents), 4),
                "p0_miss": sum(i["p0_missed"] for i in self.incidents)}


def check_p0_sla(start_s: float, first_evidence_s: float) -> dict:
    return {"start_ok": start_s <= P0_START_SLA_S, "evidence_ok": first_evidence_s <= P0_EVIDENCE_SLA_S}
