"""The evidence ledger and the read-only command policy, shared by ev.py and verify.py.

Standard library only, Python 3.9+. Layout of an evidence directory:

    .evidence/
      ledger.jsonl   one JSON object per line, hash-chained
      E1.txt         the full output of entry E1, exactly as recorded
      E2.txt ...

Each ledger line carries ``prev``: the SHA-256 of the previous line's text ("" for the first).
Editing, removing or reordering an entry, or changing an output file, breaks the chain or a
file hash, and ``verify.py`` says which entry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

LEDGER = "ledger.jsonl"
SIGNALS = ("metrics", "logs", "traces", "changes", "flags", "other")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Entry:
    raw: str  # the ledger line as written; the chain hashes this text
    data: dict

    @property
    def id(self) -> str:
        return str(self.data.get("id", ""))


def read_ledger(evdir: Path) -> list[Entry]:
    path = evdir / LEDGER
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(Entry(line, json.loads(line)))
    return out


def append_entry(evdir: Path, fields: dict, output: bytes) -> dict:
    """Write the output file, then the ledger line that names its hash."""
    evdir.mkdir(parents=True, exist_ok=True)
    entries = read_ledger(evdir)
    n = 1 + max((int(e.id[1:]) for e in entries if re.fullmatch(r"E\d+", e.id)), default=0)
    ev_id = f"E{n}"
    name = f"{ev_id}.txt"
    (evdir / name).write_bytes(output)
    record = {
        "id": ev_id,
        **fields,
        "file": name,
        "bytes": len(output),
        "sha256": sha256_bytes(output),
        "prev": sha256_bytes(entries[-1].raw.encode()) if entries else "",
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with (evdir / LEDGER).open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return record


def head(entries: list[Entry]) -> str:
    """The hash of the last line: printed after every write, so a transcript pins the ledger."""
    return sha256_bytes(entries[-1].raw.encode())[:16] if entries else "empty"


def integrity_problems(evdir: Path, entries: list[Entry]) -> list[str]:
    problems = []
    prev = ""
    seen: set[str] = set()
    for i, e in enumerate(entries):
        where = e.id or f"line {i + 1}"
        if e.data.get("prev", "") != prev:
            problems.append(f"{where}: ledger chain broken (an entry before it was edited, removed or reordered)")
        prev = sha256_bytes(e.raw.encode())
        if e.id in seen:
            problems.append(f"{where}: duplicate evidence id")
        seen.add(e.id)
        f = evdir / str(e.data.get("file", ""))
        if not f.is_file():
            problems.append(f"{where}: output file {f.name} is missing")
        elif sha256_bytes(f.read_bytes()) != e.data.get("sha256"):
            problems.append(f"{where}: output file {f.name} changed after it was recorded")
        if e.data.get("kind") == "run":
            reason = refusal(e.data.get("argv") or [])
            if reason:
                problems.append(f"{where}: recorded command is not read-only: {reason}")
    return problems


# --- Read-only policy -------------------------------------------------------------------------
#
# ev.py runs a command only if this says it cannot change anything. It is an allowlist: a binary
# or subcommand not named here is refused. The command runs without a shell, so pipes,
# redirections and `;` are never interpreted.

KUBECTL_VALUE_FLAGS = {
    "-n", "--namespace", "--context", "--cluster", "--user", "--kubeconfig", "-s", "--server",
    "-l", "--selector", "-o", "--output", "-c", "--container", "--since", "--since-time", "--tail",
    "--field-selector", "--sort-by", "--request-timeout", "--as", "--as-group", "--revision",
    "--for", "--types", "--chunk-size",
}  # fmt: skip
KUBECTL_READ = {
    "get", "describe", "logs", "top", "events", "explain", "api-resources", "api-versions",
    "version", "cluster-info",
}  # fmt: skip
KUBECTL_READ_PAIRS = {("rollout", "history"), ("rollout", "status"), ("auth", "can-i"), ("config", "get-contexts")}
KUBECTL_NEVER = {"-w", "--watch", "--watch-only", "-f", "--follow", "--raw"}  # hangs, or bypasses the verb check
CURL_WRITE = {
    "-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json", "-F", "--form",
    "--form-string", "-T", "--upload-file", "-o", "--output", "-O", "--remote-name", "-K", "--config",
}  # fmt: skip
CURL_GET_OK = {"--data-urlencode"}  # only together with -G, which turns it into a query string


def _positionals(args: list[str], value_flags: set[str]) -> list[str] | str:
    """The non-flag arguments, or why the flags cannot be parsed safely.

    A flag before the subcommand must be a known value flag or written ``--flag=value``:
    otherwise its value could be mistaken for the subcommand (``--token get delete ...``).
    """
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-"):
            if not out and a not in value_flags and "=" not in a:
                return f"unknown flag {a} before the subcommand; put it after, or write {a}=value"
            skip = a in value_flags
        else:
            out.append(a)
    return out


def _kubectl(args: list[str]) -> str | None:
    for a in args:
        if a.split("=", 1)[0] in KUBECTL_NEVER:
            return f"kubectl {a.split('=', 1)[0]} is not allowed"
    pos = _positionals(args, KUBECTL_VALUE_FLAGS)
    if isinstance(pos, str):
        return f"kubectl: {pos}"
    if not pos:
        return "kubectl needs a subcommand"
    verb = pos[0]
    if verb in KUBECTL_READ:
        if verb == "get" and any(re.match(r"^secrets?(\b|/|$)", p) for p in pos[1:]):
            return "reading Secrets is not allowed"
        return None
    if len(pos) > 1 and (verb, pos[1]) in KUBECTL_READ_PAIRS:
        return None
    return f"kubectl {verb} is not a read-only subcommand"


CURL_SAFE_SHORT = set("sSGfLkvigN46")  # boolean short flags that may be bundled, as in -sSG


def _curl(args: list[str]) -> str | None:
    get = False
    for i, a in enumerate(args):
        flag = a.split("=", 1)[0]
        if re.fullmatch(r"-X(GET|HEAD)", a, re.IGNORECASE):
            continue
        if re.fullmatch(r"-[A-Za-z0-9]{2,}", a) or (a.startswith("-") and not a.startswith("--") and len(a) > 2):
            if not set(a[1:]) <= CURL_SAFE_SHORT:
                return f"curl {a}: write bundled short flags only from -{''.join(sorted(CURL_SAFE_SHORT))}"
            get = get or "G" in a
            continue
        if flag in ("-G", "--get"):
            get = True
        elif flag in CURL_WRITE:
            return f"curl {flag} sends or writes data"
        elif flag in CURL_GET_OK and not get:
            return f"curl {flag} needs -G before it, or it sends a request body"
        elif flag in ("-X", "--request"):
            method = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            if method.upper() not in ("GET", "HEAD"):
                return f"curl -X {method} is not a read"
    urls = [a for a in args if "://" in a]
    if not urls or any(not re.match(r"^https?://", u) for u in urls):
        return "curl needs an http(s) URL"
    return None


def _first(pos: list[str] | str, allowed: set[str], tool: str) -> str | None:
    if isinstance(pos, str):
        return f"{tool}: {pos}"
    return None if pos and pos[0] in allowed else f"{tool} {pos[0] if pos else ''} is not on the read-only list"


def refusal(argv: list[str]) -> str | None:
    """None if the command is read-only by the allowlist; otherwise why it is refused."""
    if not argv:
        return "empty command"
    tool, args = os.path.basename(argv[0]), list(argv[1:])
    if tool in ("kubectl", "oc"):
        return _kubectl(args)
    if tool == "curl":
        return _curl(args)
    if tool == "promtool":
        return _first(_positionals(args, set()), {"query"}, "promtool")
    if tool == "logcli":
        return _first(_positionals(args, {"--addr", "--org-id", "--limit", "--since", "--from", "--to"}),
                      {"query", "instant-query", "labels", "series"}, "logcli")  # fmt: skip
    if tool == "helm":
        return _first(_positionals(args, {"-n", "--namespace", "--kube-context", "--max"}),
                      {"list", "ls", "status", "history"}, "helm")  # fmt: skip
    if tool == "git":
        bad = [a for a in args if a.split("=", 1)[0] in ("-c", "--config-env", "--exec-path", "--output", "--ext-diff")]
        if bad:
            return f"git {bad[0]} can run programs or write files"
        return _first(_positionals(args, {"-C", "-n", "--max-count"}), {"log", "show", "diff", "blame"}, "git")
    return f"{tool} is not on the read-only list"
