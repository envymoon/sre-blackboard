"""``python -m app.eval``: score evaluation runs, apply the release gate, write the report.

    report RESULTS...   score result files (eval/results/*.json), print the Markdown report
      --baseline FILE   the runs to compare with (default eval/baseline.json; "" for none)
      --gate V          variants that must pass the gate (default: default)
      --canary V        variants that must FAIL it (default: canary, when present), compared
                        with the same run's default variant: an evaluation that passes a
                        deliberately broken prompt measures nothing
      --json FILE       also write the metrics as JSON

    merge OUT FILES...  one result file from several (the shards of one run)
      --run ID, --commit SHA

Exit status 1 when a gated variant fails or a canary passes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from app.eval import gate, results
from app.eval.metrics import VariantMetrics, summarise, variant_metrics
from app.eval.report import render
from app.eval.score import ScenarioScore


def per_run_metrics(scores: list[ScenarioScore]) -> dict[str, list[VariantMetrics]]:
    by: dict[str, dict[str, list[ScenarioScore]]] = defaultdict(lambda: defaultdict(list))
    for s in scores:
        by[s.variant][s.run].append(s)
    out: dict[str, list[VariantMetrics]] = {}
    for v in sorted(by, key=lambda v: (v != "default", v)):
        out[v] = [variant_metrics(v, by[v][run]) for run in sorted(by[v])]
    return out


def cmd_report(args: argparse.Namespace) -> int:
    scores = results.load_scores([Path(p) for p in args.results])
    baseline_paths = results.baseline_files(Path(args.baseline)) if args.baseline else []
    baseline = [s for s in results.load_scores(baseline_paths) if s.variant == "default"]
    per_run = per_run_metrics(scores)
    canaries = args.canary if args.canary is not None else [v for v in ("canary",) if v in per_run]
    same_run = [s for s in scores if s.variant == "default"]
    gates = [gate.check(v, scores, baseline) for v in args.gate if v in per_run]
    gates += [gate.check(v, scores, same_run) for v in canaries if v in per_run]
    print(render(scores, per_run, gates), end="")

    status = 0
    for g in gates:
        if g.variant in canaries and g.passed:
            print(f"\nevaluation void: the canary variant {g.variant!r} passed the gate")
            status = 1
        elif g.variant not in canaries and not g.passed:
            status = 1
    missing = [v for v in args.gate if v not in per_run]
    if missing:
        print(f"\nno results for gated variant(s): {', '.join(missing)}")
        status = 1
    if args.json:
        doc = {
            "variants": {
                v: {
                    "runs": [m.model_dump() for m in ms],
                    "summary": {k: st.model_dump() if st else None for k, st in summarise(ms).items()},
                }
                for v, ms in per_run.items()
            },
            "gate": {g.variant: {"passed": g.passed, "reasons": g.reasons} for g in gates},
        }
        Path(args.json).write_text(json.dumps(doc, indent=1) + "\n")
    return status


def cmd_merge(args: argparse.Namespace) -> int:
    merged = results.ResultFile(run=args.run, kind="live", commit=args.commit)
    for path in args.files:
        part = results.ResultFile.load(Path(path))
        merged.kind = part.kind
        merged.observations += [o if o.run else o.model_copy(update={"run": args.run}) for o in part.observations]
    merged.observations.sort(key=lambda o: (o.scenario, o.variant))
    merged.dump(Path(args.out))
    print(f"{len(merged.observations)} observation(s) from {len(args.files)} file(s) -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.eval", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("report")
    p.add_argument("results", nargs="+")
    p.add_argument("--baseline", default=str(results.BASELINE))
    p.add_argument("--gate", nargs="*", default=["default"])
    p.add_argument("--canary", nargs="*", default=None)
    p.add_argument("--json")
    p.set_defaults(func=cmd_report)
    p = sub.add_parser("merge")
    p.add_argument("out")
    p.add_argument("files", nargs="+")
    p.add_argument("--run", required=True)
    p.add_argument("--commit", default="")
    p.set_defaults(func=cmd_merge)
    args = parser.parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
