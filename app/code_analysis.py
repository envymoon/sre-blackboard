"""Code Analysis: smallest reusable evidence unit. Maps to spec Sec.13.0.1: read-only;
returns NOT_APPLICABLE without a revision/stack-frame/symbol trigger."""
from __future__ import annotations

LIMITS = {"model_calls": 2, "format_fix": 1, "search": 4, "files": 12, "lines": 1500,
          "timeout_s": 120, "input": 18000, "output": 2000}


def analyze(change_id: str = "", stack: str = "", symbol: str = "", files_hit: int = 0, failures: int = 0) -> dict:
    if not (change_id or stack or symbol):
        return {"status": "NOT_APPLICABLE"}
    if failures >= 2:
        return {"status": "CODE_EVIDENCE_UNAVAILABLE"}
    if files_hit > LIMITS["files"]:
        return {"status": "CODE_EVIDENCE_UNAVAILABLE", "reason": "file budget exceeded"}
    return {"status": "OK", "revision_pair": ["2026.09.14.9", "2026.09.15.3"],
            "changed_symbols": [symbol or "deductInventory"],
            "file_line_refs": ["inventory.ts:142"],
            "diff_evidence": ["CODE-4402"],
            "causal_assessment": "LIKELY",
            "gaps": [], "repository": "read_only", "shell": "disabled"}
