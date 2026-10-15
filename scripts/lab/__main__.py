"""Fault lab CLI: ``python -m scripts.lab <command>`` (wrapped by make targets).

list                       the fault catalogue
inject NAME / recover NAME break or repair one thing (recover --all undoes everything)
status                     active faults and firing alerts
probe                      per-service SLIs right now, to tune alert thresholds
accept [--tag T | NAMES]   the M2 acceptance run (see scripts/lab/accept.py)
env                        settings for the read-only tools (make lab-env writes them to .lab.env)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from scripts.lab import accept
from scripts.lab.cluster import Lab
from scripts.lab.faults import CATALOGUE, get

# The read-only ServiceAccount for the tools (deploy/kind/manifests/reader.yaml).
READER = "sre-blackboard-reader"


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
    )
    markdown = accept.to_markdown(results, args.alert_timeout)
    print("\n" + markdown)
    if args.report:
        with open(args.report, "w") as fh:
            fh.write(accept.to_json(results, args.alert_timeout))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write("### Fault lab acceptance\n\n" + markdown)
    return 0 if all(r.ok(args.alert_timeout) for r in results) else 1


def cmd_env(args: argparse.Namespace) -> int:
    """Settings for the read-only tools (app/tools): print them as .lab.env lines.

    The Kubernetes token belongs to the reader ServiceAccount (deploy/kind/manifests/reader.yaml),
    which can only get and list in the demo namespace, and expires after --ttl.
    """
    kubectl = _lab(args).kubectl
    config = json.loads(kubectl("config", "view", "--minify", "--raw", "-o", "json"))
    cluster = config["clusters"][0]["cluster"]
    ca = Path(args.ca_file)
    ca.write_bytes(base64.b64decode(cluster["certificate-authority-data"]))
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
    print("\n".join(f"{k}={v}" for k, v in lines.items()))
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
    p.set_defaults(func=cmd_accept)

    args = parser.parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
