"""Build the evidence-cited-investigation skill's example ledger from a real lab recording.

    uv run python scripts/build_skill_example.py

Source: tests/fixtures/lab/bad-release.jsonl.gz, the tool calls recorded in lab run
38672622429 while checkout's bad release was live. Each result becomes an `added` entry whose
source names the run, the tool and its arguments; the two largest are trimmed (first trace of
20, first 12 log lines of 50) and the trim is stated in the entry's note. deploy_diff is left
out: it returned the same Deployment object as rollout_history.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "evidence-cited-investigation"
sys.path.insert(0, str(SKILL / "scripts"))
from ledger import append_entry  # type: ignore[import-not-found]  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "lab" / "bad-release.jsonl.gz"
OUT = SKILL / "examples" / "bad-release" / ".evidence"
RUN = "38672622429"
SIGNAL = {
    "prom_query": "metrics",
    "rollout_history": "changes",
    "k8s_events": "changes",
    "traces_search": "traces",
    "logs_search": "logs",
}


def main() -> None:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    if OUT.exists():
        shutil.rmtree(OUT)
    for row in rows:
        tool = row.get("tool")
        if tool not in SIGNAL:
            continue
        raw, note = row["raw"], None
        if tool == "traces_search":
            note = f"trimmed: first trace of {len(raw)}"
            raw = raw[:1]
        if tool == "logs_search":
            values = raw["result"][0]["values"]
            note = f"trimmed: first 12 log lines of {len(values)}"
            raw["result"][0]["values"] = values[:12]
        args = json.dumps(row["args"], sort_keys=True)
        fields = {
            "kind": "added",
            "source": f"sre-blackboard lab run {RUN}: {tool} {args}",
            "signal": SIGNAL[tool],
            "at": row["at"],
        }
        if note:
            fields["note"] = note
        append_entry(OUT, fields, (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode())
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
