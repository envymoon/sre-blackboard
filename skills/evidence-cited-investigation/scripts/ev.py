#!/usr/bin/env python3
"""Record evidence for an investigation: run a read-only query, keep its output, give it an id.

    ev.py run   --signal metrics -- curl -sG http://prometheus:9090/api/v1/query --data-urlencode 'query=up'
    ev.py add   --signal logs --source "Grafana Explore export, pasted by the user" export.txt
    ev.py show  E3 [--grep TEXT] [--lines 40:80]
    ev.py list

`run` refuses anything the read-only allowlist does not name (see ledger.py) and runs the
command without a shell. `add` records evidence obtained some other way (a tool call, a file,
a paste) and marks it as such: the verifier can check that it was not changed afterwards, not
that it came from where its source says.

Every write prints the ledger head hash; the last one in the transcript pins the whole ledger.
The evidence directory is ./.evidence unless --dir or $EVIDENCE_DIR says otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import SIGNALS, append_entry, head, read_ledger, refusal

SHOW_LINES = 120
SHOW_CHARS = 12_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: UP017 (3.9+)


def _print_output(text: str, ev_id: str, max_lines: int = SHOW_LINES) -> None:
    lines = text.splitlines()
    shown = "\n".join(lines[:max_lines])[:SHOW_CHARS]
    print(shown)
    if len(shown) < len(text.rstrip("\n")):
        print(
            f"... [{len(lines)} lines, {len(text)} chars in total: "
            f"`ev.py show {ev_id} --grep TEXT` or `--lines A:B` for the rest]"
        )


def _written(evdir: Path, record: dict) -> None:
    entries = read_ledger(evdir)
    extra = f"exit {record['exit']}  " if "exit" in record else ""
    print(f"== {record['id']}  {record['bytes']} bytes  {extra}ledger head {head(entries)}")


def cmd_run(a: argparse.Namespace, evdir: Path) -> int:
    argv = a.argv[1:] if a.argv[:1] == ["--"] else a.argv
    reason = refusal(argv)
    if reason:
        print(f"refused: {reason}. Nothing was run or recorded.", file=sys.stderr)
        return 2
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=a.timeout, stdin=subprocess.DEVNULL)
        out, code = proc.stdout + (b"\n[stderr]\n" + proc.stderr if proc.stderr else b""), proc.returncode
    except FileNotFoundError:
        print(f"refused: {argv[0]} is not installed. Nothing was recorded.", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as e:
        out, code = (e.stdout or b"") + f"\n[timed out after {a.timeout}s]".encode(), -1
    fields = {
        "kind": "run",
        "argv": argv,
        "signal": a.signal,
        "at": _now(),
        "seconds": round(time.monotonic() - started, 2),
        "exit": code,
    }
    if a.note:
        fields["note"] = a.note
    record = append_entry(evdir, fields, out)
    _written(evdir, record)
    _print_output(out.decode("utf-8", "replace"), record["id"])
    return 0


def cmd_add(a: argparse.Namespace, evdir: Path) -> int:
    data = sys.stdin.buffer.read() if a.file == "-" else Path(a.file).read_bytes()
    fields = {"kind": "added", "source": a.source, "signal": a.signal, "at": _now()}
    if a.note:
        fields["note"] = a.note
    record = append_entry(evdir, fields, data)
    _written(evdir, record)
    return 0


def cmd_show(a: argparse.Namespace, evdir: Path) -> int:
    entry = next((e for e in read_ledger(evdir) if e.id == a.id), None)
    if entry is None:
        print(f"no evidence {a.id} in {evdir}", file=sys.stderr)
        return 1
    text = (evdir / entry.data["file"]).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if a.grep:
        rx = re.compile(re.escape(a.grep), re.IGNORECASE)
        hits = [f"{i + 1}: {line[:400]}" for i, line in enumerate(lines) if rx.search(line)]
        print("\n".join(hits[:200]) or f"no line of {a.id} contains {a.grep!r}")
        return 0
    if a.lines:
        lo, _, hi = a.lines.partition(":")
        start, end = max(int(lo or 1), 1), int(hi or len(lines))
        print("\n".join(f"{i}: {lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1)))
        return 0
    _print_output(text, a.id)
    return 0


def cmd_list(_: argparse.Namespace, evdir: Path) -> int:
    entries = read_ledger(evdir)
    for e in entries:
        d = e.data
        what = " ".join(d["argv"]) if d.get("kind") == "run" else f"(added) {d.get('source', '')}"
        print(f"{e.id:>4}  {d.get('signal', ''):8} {d.get('bytes', 0):>8}  {what[:110]}")
    print(f"{len(entries)} entries, ledger head {head(entries)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", default=os.environ.get("EVIDENCE_DIR", ".evidence"))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a read-only command and record its output")
    r.add_argument("--signal", choices=SIGNALS, required=True)
    r.add_argument("--note", help="why you ran it, in a few words")
    r.add_argument("--timeout", type=int, default=60)
    r.add_argument("argv", nargs=argparse.REMAINDER)
    ad = sub.add_parser("add", help="record evidence obtained another way")
    ad.add_argument("--signal", choices=SIGNALS, required=True)
    ad.add_argument("--source", required=True, help="where it came from: tool name and query, or who pasted it")
    ad.add_argument("--note")
    ad.add_argument("file", help="file with the content, or - for stdin")
    s = sub.add_parser("show", help="print a recorded output")
    s.add_argument("id")
    s.add_argument("--grep")
    s.add_argument("--lines", help="A:B, 1-based and inclusive")
    sub.add_parser("list", help="list the ledger")
    a = p.parse_args(argv)
    evdir = Path(a.dir)
    return {"run": cmd_run, "add": cmd_add, "show": cmd_show, "list": cmd_list}[a.cmd](a, evdir)


if __name__ == "__main__":
    sys.exit(main())
