"""The evidence-cited-investigation skill: read-only policy, ledger integrity, report checks."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "evidence-cited-investigation"
EXAMPLE = SKILL / "examples" / "bad-release"
sys.path.insert(0, str(SKILL / "scripts"))

import ledger  # noqa: E402
import verify  # noqa: E402

ALLOWED = [
    "kubectl get pods -n shop",
    "kubectl -n shop get pods",
    "kubectl -n shop rollout history deploy/checkout --revision=2",
    "kubectl -n shop events --for deploy/checkout",
    "kubectl -n shop get events --sort-by=.lastTimestamp",
    "kubectl -n shop logs pod-1 --previous --tail=200",
    "kubectl auth can-i get pods",
    "curl -sG http://prom:9090/api/v1/query --data-urlencode query=up",
    "curl -s -XGET https://jaeger/api/services",
    "promtool query instant http://prom:9090 up",
    "logcli --addr=http://loki:3100 query --since=30m {app=x}",
    "helm -n shop history checkout",
    "git -C repo log --since=6.hours --stat",
]
REFUSED = [
    ("kubectl delete pod a", "not a read-only"),
    ("kubectl rollout undo deploy/checkout", "not a read-only"),
    ("kubectl scale deploy/a --replicas=0", "not a read-only"),
    ("kubectl exec pod -- sh", "not a read-only"),
    ("kubectl --token get delete pod a", "unknown flag"),
    ("kubectl get secret db -o yaml", "Secrets"),
    ("kubectl logs -f pod", "-f"),
    ("kubectl get --raw /api/v1/namespaces", "--raw"),
    ("curl -s http://prom/api/v1/query --data-urlencode query=up", "needs -G"),
    ("curl -X POST http://a", "not a read"),
    ("curl -XDELETE http://a", "bundled"),
    ("curl -sd x http://a", "bundled"),
    ("curl -d @body http://a", "sends or writes"),
    ("curl -s http://a -o /tmp/x", "sends or writes"),
    ("curl file:///etc/passwd", "http(s)"),
    ("git -c core.fsmonitor=x log", "run programs"),
    ("git push", "read-only list"),
    ("helm rollback checkout 1", "read-only list"),
    ("sh -c 'kubectl get pods'", "read-only list"),
    ("rm -rf /", "read-only list"),
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_policy_allows_reads(cmd: str) -> None:
    assert ledger.refusal(cmd.split()) is None


@pytest.mark.parametrize(("cmd", "why"), REFUSED)
def test_policy_refuses_writes(cmd: str, why: str) -> None:
    reason = ledger.refusal(cmd.split())
    assert reason is not None and why in reason


def _ev(evdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "ev.py"), "--dir", str(evdir), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_records_and_refuses(tmp_path: Path) -> None:
    evdir = tmp_path / ".evidence"
    ok = _ev(evdir, "run", "--signal", "changes", "--", "git", "-C", str(SKILL), "log", "-1", "--format=%H")
    assert ok.returncode == 0 and ok.stdout.startswith("== E1 ")
    refused = _ev(evdir, "run", "--signal", "changes", "--", "git", "push")
    assert refused.returncode == 2 and "refused" in refused.stderr
    entries = ledger.read_ledger(evdir)
    assert [e.id for e in entries] == ["E1"]
    assert entries[0].data["kind"] == "run" and entries[0].data["argv"][-1] == "--format=%H"
    added = _ev(evdir, "add", "--signal", "logs", "--source", "pasted by the user", str(SKILL / "SKILL.md"))
    assert added.returncode == 0
    assert ledger.integrity_problems(evdir, ledger.read_ledger(evdir)) == []


def test_example_report_passes() -> None:
    res = verify.verify((EXAMPLE / "report.md").read_text(), EXAMPLE / ".evidence")
    assert res.errors == []
    assert res.claims == 9 and res.quotes_checked >= 20
    # The example's evidence was imported, not run: the verifier says so.
    assert all("added evidence" in w for w in res.warnings)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("invented-id.md", "E7 is not in the ledger"),
        ("wrong-number.md", "`0.96` is not in E1"),
        ("misquote.md", "`connection refused` is not in E4"),
        ("wrong-source.md", "`payment:9999` is not in E1"),
        ("uncited.md", "cites no evidence"),
    ],
)
def test_broken_reports_fail(name: str, expected: str) -> None:
    res = verify.verify((EXAMPLE / "broken" / name).read_text(), EXAMPLE / ".evidence")
    assert any(expected in e for e in res.errors), res.errors


def _copy(tmp_path: Path) -> Path:
    evdir = tmp_path / ".evidence"
    shutil.copytree(EXAMPLE / ".evidence", evdir)
    return evdir


def test_edited_output_is_detected(tmp_path: Path) -> None:
    evdir = _copy(tmp_path)
    f = evdir / "E1.txt"
    f.write_text(f.read_text().replace("0.6896795379350099", "0.9896795379350099"))
    res = verify.verify((EXAMPLE / "report.md").read_text(), evdir)
    assert "E1: output file E1.txt changed after it was recorded" in res.errors


def test_edited_or_removed_ledger_line_is_detected(tmp_path: Path) -> None:
    evdir = _copy(tmp_path)
    lines = (evdir / "ledger.jsonl").read_text().splitlines()
    first = json.loads(lines[0])
    first["source"] = "something else"
    lines[0] = json.dumps(first, sort_keys=True)
    (evdir / "ledger.jsonl").write_text("\n".join(lines) + "\n")
    res = verify.verify((EXAMPLE / "report.md").read_text(), evdir)
    assert any(e.startswith("E2: ledger chain broken") for e in res.errors)

    evdir = _copy(tmp_path / "again")
    lines = (evdir / "ledger.jsonl").read_text().splitlines()
    (evdir / "ledger.jsonl").write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    res = verify.verify((EXAMPLE / "report.md").read_text(), evdir)
    assert any(e.startswith("E4: ledger chain broken") for e in res.errors)


def test_recorded_write_command_is_detected(tmp_path: Path) -> None:
    evdir = tmp_path / ".evidence"
    ledger.append_entry(evdir, {"kind": "run", "argv": ["kubectl", "delete", "pod", "a"], "signal": "other"}, b"x")
    res = verify.verify("- [undetermined] nothing yet", evdir)
    assert any("not read-only" in e for e in res.errors)


@pytest.mark.parametrize(
    ("quote", "found"),
    [
        ("0.69", True),
        ("0.7", True),
        ("0.690", True),
        ("69%", True),
        ("0.68", False),
        ("1", False),  # a whole number does not stand for a rounded fraction
        ("10.96.201.161:9999", True),
        ('"transport: Error while dialing', True),  # JSON escaping undone
        ("connection refused", False),
    ],
)
def test_quote_matching(quote: str, found: bool) -> None:
    ev = "E1.txt" if verify.NUMBER.match(quote) else "E4.txt"  # E1: the error ratios; E4: a trace
    raw = (EXAMPLE / ".evidence" / ev).read_text()
    assert verify.quote_found(quote, raw) is found


def test_claim_parsing() -> None:
    report = "\n".join(
        [
            "- [fact] a `1` [E1, E2].",
            "* [hypothesis] b, confirm with `kubectl get svc`",
            "- [guess] c [E1]",
            "```",
            "- [fact] inside a code block [E9]",
            "```",
        ]
    )
    claims, errors = verify.parse_claims(report)
    assert [(c.kind, c.cites) for c in claims] == [("fact", ["E1", "E2"]), ("hypothesis", [])]
    assert errors == ["line 3: unknown claim kind [guess]; use one of " + ", ".join(verify.KINDS)]
