"""Fault lab CLI: ``python -m scripts.lab <command>`` (wrapped by make targets).

list                       the fault catalogue
inject NAME / recover NAME break or repair one thing (recover --all undoes everything)
status                     active faults and firing alerts
probe                      per-service SLIs right now, to tune alert thresholds
accept [--tag T | NAMES]   the M2 acceptance run (see scripts/lab/accept.py)
eval [--shard i/n | IDS]   the M7 evaluation over eval/scenarios (see scripts/lab/evaluate.py)
env                        settings for the read-only tools (make lab-env writes them to .lab.env)
fixtures DIR --run ID      turn a lab-recordings branch checkout into the contract test fixtures
eval-import DIR --run ID   an evaluation's results and recordings into eval/ (the CI replay and baseline)
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
from pathlib import Path
from typing import Any

from scripts.lab import accept
from scripts.lab.cluster import Lab
from scripts.lab.faults import CATALOGUE, get

# The read-only ServiceAccount for the tools (deploy/kind/manifests/reader.yaml).
READER = "sre-blackboard-reader"
# The executor's ServiceAccount: patch Deployments in the demo namespace (deploy/kind/manifests/executor.yaml).
EXECUTOR = "sre-blackboard-executor"


def _lab(args: argparse.Namespace) -> Lab:
    return Lab(base_url=args.lab_url, prometheus_url=args.prometheus_url)


def cmd_list(_: argparse.Namespace) -> int:
    width = max(len(f.name) for f in CATALOGUE)
    for f in CATALOGUE:
        tags = f" [{', '.join(f.tags)}]" if f.tags else ""
        print(f"{f.name:<{width}}  {f.kind:<7}  root cause: {f.root_cause:<16} {f.description}{tags}")
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    fault = get(args.name)
    _lab(args).inject(fault)
    print(f"injected {fault.name}: {fault.description}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    lab = _lab(args)
    faults = lab.active() if args.all else [get(args.name)]
    for fault in faults:
        print(f"{'recovered' if lab.recover(fault) else 'not active'}: {fault.name}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    lab = _lab(args)
    active = lab.active()
    print("active faults:", ", ".join(f.name for f in active) or "none")
    firing = lab.firing_alerts()
    print("firing alerts:", "none" if not firing else "")
    for a in firing:
        labels = a["labels"]
        print(f"  {labels.get('alertname')} service={labels.get('service_name')} severity={labels.get('severity')}")
    return 0


PROBES = {
    "req/s": "service:sli_requests:rate5m",
    "error ratio": "service:sli_error_ratio:rate5m",
    "p95 ms": "service:sli_latency_p95_ms:rate5m",
}


def cmd_probe(args: argparse.Namespace) -> int:
    lab = _lab(args)
    table: dict[str, dict[str, str]] = {}
    for column, promql in PROBES.items():
        for sample in lab.query(promql):
            service = sample["metric"].get("service_name", "?")
            table.setdefault(service, {})[column] = f"{float(sample['value'][1]):.3g}"
    print(f"{'service':<22}" + "".join(f"{c:>14}" for c in PROBES))
    for service in sorted(table):
        print(f"{service:<22}" + "".join(f"{table[service].get(c, '-'):>14}" for c in PROBES))
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    from sqlalchemy import create_engine

    from app.core.config import get_settings

    faults = [get(n) for n in args.names] if args.names else [f for f in CATALOGUE if args.tag in f.tags]
    engine = create_engine(get_settings().database_url)
    lab = _lab(args)
    if args.baseline > 0:
        print(f"[baseline] watching for {args.baseline:.0f}s with no fault injected")
        if noise := accept.check_baseline(lab, args.baseline, print):
            message = f"FAIL: alerts on a healthy lab: {', '.join(noise)}\n"
            print(message)
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(summary, "a") as fh:
                    fh.write("### Fault lab acceptance\n\n" + message)
            return 1
        print("[baseline] quiet")
    probe = None
    if args.probe_dir:
        from scripts.lab.probes import ProbeRunner

        probe = ProbeRunner(get_settings(), Path(args.probe_dir), tenant=args.tenant)
    diagnoser = None
    if args.diagnose:
        from scripts.lab.diagnose import Diagnoser

        out = Path(args.record_agents) if args.record_agents else None
        diagnoser = Diagnoser(
            engine,
            get_settings().database_url,
            args.tenant,
            out,
            timeout_s=args.diagnose_timeout,
            planning=args.remediate,
        )
    remediator = None
    if args.remediate:
        from scripts.lab.remediate import Remediator

        cases = Path(args.record_agents) / "cases" if args.record_agents else None
        remediator = Remediator(engine, get_settings(), args.tenant, api_url=args.api_url, cases=cases)
    results = accept.run(
        lab,
        engine,
        faults,
        tenant=args.tenant,
        alert_timeout=args.alert_timeout,
        settle_s=args.settle,
        resolve_timeout=args.resolve_timeout,
        hold_s=args.hold,
        probe=probe,
        diagnoser=diagnoser,
        remediator=remediator,
    )
    markdown = accept.to_markdown(results, args.alert_timeout)
    chain_ok = True
    if remediator is not None:
        chain = remediator.verify_audit()
        chain_ok = bool(chain["ok"])
        markdown += (
            f"\nAudit chain: {'ok' if chain_ok else 'BROKEN at seq ' + str(chain['first_bad_seq'])}, "
            f"{chain['checked']} rows verified\n"
        )
    print("\n" + markdown)
    if args.report:
        with open(args.report, "w") as fh:
            fh.write(accept.to_json(results, args.alert_timeout))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write("### Fault lab acceptance\n\n" + markdown)
    ok = all(r.ok(args.alert_timeout) for r in results) and chain_ok
    if diagnoser is not None:
        ok = ok and sum(r.diagnosis_correct for r in results) >= accept.required_correct(len(results))
    return 0 if ok else 1


def cmd_eval(args: argparse.Namespace) -> int:
    from sqlalchemy import create_engine

    from app.core.config import get_settings
    from app.eval import scenarios
    from scripts.lab.diagnose import Diagnoser
    from scripts.lab.evaluate import run, shard
    from scripts.lab.remediate import Remediator

    settings = get_settings()
    picked = [scenarios.get(n) for n in args.ids] if args.ids else shard(scenarios.catalogue(), args.shard)
    print(f"[eval] {len(picked)} scenario(s): {', '.join(s.id for s in picked)}")
    variants = dict(settings.eval_tenant_variants)
    print(f"[eval] variants: default ({args.tenant}), " + ", ".join(f"{v} ({t})" for t, v in variants.items()))
    engine = create_engine(settings.database_url)
    lab = _lab(args)
    out = Path(args.out)
    if args.baseline > 0:
        print(f"[baseline] watching for {args.baseline:.0f}s with no fault injected")
        if noise := accept.check_baseline(lab, args.baseline, print):
            print(f"[baseline] alerts on a healthy lab: {', '.join(noise)} (the run goes on; scores may suffer)")
    diagnoser = Diagnoser(
        engine,
        settings.database_url,
        args.tenant,
        out / "agent",
        timeout_s=args.diagnose_timeout,
        planning=True,
        variants=variants,
    )
    remediator = Remediator(engine, settings, args.tenant, api_url=args.api_url, cases=out / "agent" / "cases")
    results = run(
        lab,
        engine,
        picked,
        diagnoser=diagnoser,
        remediator=remediator,
        database_url=settings.database_url,
        tenant=args.tenant,
        out=out,
        run_id=args.run,
        commit=args.commit,
        alert_timeout=args.alert_timeout,
        deadline=args.deadline,
        token_budget=args.token_budget,
        settle_s=args.settle,
        resolve_timeout=args.resolve_timeout,
        hold_s=args.hold,
    )
    print(f"[eval] {len(results.observations)} observation(s) in {out / 'results.json'}")
    return 0


# Inside kind's Docker network the node answers by its container name: the NodePorts the
# host reaches through port mappings, and the API server (its certificate covers that name).
NODE = "sre-lab-control-plane"
INTERNAL = {
    "SRE_PROMETHEUS_URL": f"http://{NODE}:30090",
    "SRE_LOKI_URL": f"http://{NODE}:30100",
    "SRE_JAEGER_URL": f"http://{NODE}:30080/jaeger/ui",
    "SRE_FLAGD_UI_URL": f"http://{NODE}:30080/feature",
    "SRE_K8S_API_URL": f"https://{NODE}:6443",
}


def cmd_env(args: argparse.Namespace) -> int:
    """Settings for the read-only tools (app/tools): print them as .lab.env lines.

    The Kubernetes token belongs to the reader ServiceAccount (deploy/kind/manifests/reader.yaml),
    which can only get and list in the demo namespace, and expires after --ttl.

    With --internal, the endpoints are for a container on kind's Docker network (the compose
    workers, deploy/kind/compose.lab.yml), and the CA file is named as mounted there.

    With --executor, only the Kubernetes API and a token for the executor's ServiceAccount
    (deploy/kind/manifests/executor.yaml), which may patch Deployments in the namespace (M5).
    """
    kubectl = _lab(args).kubectl
    config = json.loads(kubectl("config", "view", "--minify", "--raw", "-o", "json"))
    cluster = config["clusters"][0]["cluster"]
    ca = Path(args.ca_file)
    ca.write_bytes(base64.b64decode(cluster["certificate-authority-data"]))
    if args.executor:
        # Only what the executor needs: the API server and its own token. No read tools.
        token = kubectl("create", "token", EXECUTOR, f"--duration={args.ttl}").strip()
        lines = {
            "SRE_K8S_API_URL": INTERNAL["SRE_K8S_API_URL"] if args.internal else cluster["server"],
            "SRE_K8S_CA_FILE": "/run/lab/ca.crt" if args.internal else str(ca.resolve()),
            "SRE_K8S_NAMESPACE": kubectl.namespace,
            "SRE_EXECUTOR_K8S_TOKEN": token,
        }
        print("\n".join(f"{k}={v}" for k, v in lines.items()))
        return 0
    token = kubectl("create", "token", READER, f"--duration={args.ttl}").strip()
    lines = {
        "SRE_PROMETHEUS_URL": args.prometheus_url,
        "SRE_LOKI_URL": "http://localhost:13100",
        "SRE_JAEGER_URL": f"{args.lab_url}/jaeger/ui",
        "SRE_FLAGD_UI_URL": f"{args.lab_url}/feature",
        "SRE_K8S_API_URL": cluster["server"],
        "SRE_K8S_CA_FILE": str(ca.resolve()),
        "SRE_K8S_NAMESPACE": kubectl.namespace,
        "SRE_K8S_TOKEN": token,
    }
    if args.internal:
        lines |= INTERNAL | {"SRE_K8S_CA_FILE": "/run/lab/ca.crt"}
    print("\n".join(f"{k}={v}" for k, v in lines.items()))
    return 0


def cmd_fixtures(args: argparse.Namespace) -> int:
    """Copy a run's cassettes and what each probe saw live into tests/fixtures/lab.

    The contract tests replay the cassettes and must reproduce the live results exactly.
    """
    src, out = Path(args.dir), Path(args.out)
    report = json.loads((src / "report.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    expected: dict[str, list[dict[str, object]]] = {}
    for result in report:
        fault = result["fault"]
        cassette = next((src / "cassettes").glob(f"{fault}.jsonl*"), None)
        if cassette is None or not result["probes"]:
            continue
        data = cassette.read_bytes()
        if cassette.suffix != ".gz":
            data = gzip.compress(data, mtime=0)
        (out / f"{fault}.jsonl.gz").write_bytes(data)
        expected[fault] = [{k: p[k] for k in ("name", "tool", "ok", "detail", "evidence_id")} for p in result["probes"]]
        print(f"{fault}: {len(data)} bytes, {sum(p['ok'] for p in result['probes'])}/{len(result['probes'])} ok")
    manifest = {"run": args.run, "faults": expected}
    (out / "expected.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_eval_import(args: argparse.Namespace) -> int:
    """Copy one live evaluation (a lab-recordings/eval-<id> checkout) into eval/.

    The observations go to eval/results/run-<id>.json; with --baseline the run is added to
    eval/baseline.json. The recordings of the default variant, and of the variants named
    with --variants, replace eval/recordings (what CI replays on every pull request). With
    --add only the --variants recordings are copied, beside the ones already there: a rerun
    of one variant (a fixed canary) joins the run whose default it is gated against.
    """
    import shutil

    src, repo = Path(args.dir), Path(args.repo)
    result = src / f"run-{args.run}.json"
    results_dir, recordings = repo / "eval" / "results", repo / "eval" / "recordings"
    results_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(result, results_dir / result.name)
    print(f"observations: {results_dir / result.name}")
    if args.baseline:
        baseline = repo / "eval" / "baseline.json"
        doc: dict[str, Any] = (
            json.loads(baseline.read_text()) if baseline.exists() else {"variant": "default", "results": []}
        )
        entry = f"eval/results/{result.name}"
        if entry not in doc["results"]:
            doc["results"].append(entry)
        baseline.write_text(json.dumps(doc, indent=2) + "\n")
        print(f"baseline: {', '.join(doc['results'])}")
    if recordings.exists() and not args.add:
        shutil.rmtree(recordings)
    keep = set(args.variants)
    for scenario in sorted(p for p in (src / "agent").iterdir() if p.is_dir() and p.name != "cases"):
        if (scenario / "incidents.json").exists() and not args.add:
            out = recordings / scenario.name
            out.mkdir(parents=True)
            for f in ("incidents.json", "llm.jsonl.gz", "tools.jsonl.gz"):
                shutil.copyfile(scenario / f, out / f)
        for variant in sorted(p for p in scenario.iterdir() if p.is_dir() and p.name in keep):
            out = recordings / scenario.name / variant.name
            if out.exists():
                shutil.rmtree(out)
            shutil.copytree(variant, out)
    size = sum(f.stat().st_size for f in recordings.rglob("*") if f.is_file()) if recordings.exists() else 0
    print(f"recordings: {recordings} ({size / 1e6:.1f} MB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.lab", description=__doc__.split("\n")[0])
    parser.add_argument("--lab-url", default=os.environ.get("LAB_URL", "http://localhost:18080"))
    parser.add_argument("--prometheus-url", default=os.environ.get("LAB_PROMETHEUS_URL", "http://localhost:19090"))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)
    p = sub.add_parser("inject")
    p.add_argument("name")
    p.set_defaults(func=cmd_inject)
    p = sub.add_parser("recover")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("name", nargs="?")
    group.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_recover)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("probe").set_defaults(func=cmd_probe)
    p = sub.add_parser("env", help="settings for the read-only tools, as .lab.env lines")
    p.add_argument("--ca-file", default=".lab-ca.crt")
    p.add_argument("--ttl", default="12h")
    p.add_argument("--internal", action="store_true", help="endpoints for a container on kind's network")
    p.add_argument("--executor", action="store_true", help="the executor's settings and token instead (M5)")
    p.set_defaults(func=cmd_env)
    p = sub.add_parser("accept")
    p.add_argument("names", nargs="*", help="faults to run (default: those tagged --tag)")
    p.add_argument("--tag", default="acceptance")
    p.add_argument("--tenant", default=os.environ.get("SRE_LAB_TENANT", "lab"))
    p.add_argument("--alert-timeout", type=float, default=300)
    p.add_argument("--settle", type=float, default=60)
    p.add_argument("--resolve-timeout", type=float, default=600)
    p.add_argument(
        "--baseline", type=float, default=600, help="seconds a healthy lab must stay alert-free first (0: skip)"
    )
    p.add_argument("--hold", type=float, default=60, help="seconds the lab must stay quiet to count as quiet")
    p.add_argument("--report", help="write results as JSON here")
    p.add_argument(
        "--probe-dir", help="call the read-only tools during each fault, check them, record cassettes here (M3)"
    )
    p.add_argument("--diagnose", action="store_true", help="wait for the agents' diagnosis of each fault (M4)")
    p.add_argument("--diagnose-timeout", type=float, default=900)
    p.add_argument("--record-agents", help="export each fault's investigations as a recording here")
    p.add_argument(
        "--remediate",
        action="store_true",
        help="M5: approve each fault's known repair through the API and wait for the executor and the verifier",
    )
    p.add_argument("--api-url", default="http://localhost:8000")
    p.set_defaults(func=cmd_accept)
    p = sub.add_parser("eval", help="the M7 evaluation over eval/scenarios")
    p.add_argument("ids", nargs="*", help="scenario ids (default: all, or the --shard)")
    p.add_argument("--shard", help="i/n: every n-th scenario starting with the i-th")
    p.add_argument("--tenant", default=os.environ.get("SRE_LAB_TENANT", "lab"))
    p.add_argument("--out", default="lab-out/eval")
    p.add_argument("--run", default="local", help="the Actions run id, kept with every observation")
    p.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    p.add_argument("--baseline", type=float, default=300)
    p.add_argument("--alert-timeout", type=float, default=300)
    p.add_argument("--settle", type=float, default=60)
    p.add_argument("--resolve-timeout", type=float, default=600)
    p.add_argument("--hold", type=float, default=60)
    p.add_argument("--diagnose-timeout", type=float, default=1500)
    p.add_argument(
        "--deadline", type=float, help="epoch seconds the run must end by (waits for a usage window only before it)"
    )
    p.add_argument("--token-budget", type=int, help="model tokens this run may use; no scenario starts after that")
    p.add_argument("--api-url", default="http://localhost:8000")
    p.set_defaults(func=cmd_eval)
    p = sub.add_parser("fixtures", help="contract test fixtures from a lab-recordings checkout")
    p.add_argument("dir")
    p.add_argument("--run", required=True, help="the Actions run id, kept in the manifest")
    p.add_argument("--out", default="tests/fixtures/lab")
    p.set_defaults(func=cmd_fixtures)

    p = sub.add_parser("eval-import", help="an evaluation run's results and recordings into eval/")
    p.add_argument("dir", help="a checkout of lab-recordings/eval-<run id>")
    p.add_argument("--run", required=True)
    p.add_argument("--repo", default=".")
    p.add_argument("--baseline", action="store_true", help="add the run to eval/baseline.json")
    p.add_argument("--variants", nargs="*", default=[], help="also keep these variants' recordings")
    p.add_argument("--add", action="store_true", help="add the --variants recordings to eval/recordings, keep the rest")
    p.set_defaults(func=cmd_eval_import)

    args = parser.parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
