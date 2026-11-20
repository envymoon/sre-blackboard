#!/usr/bin/env python3
"""Check an investigation report against its evidence ledger. Deterministic: no model involved.

    verify.py report.md [--dir .evidence] [--json]

Claims are the report's bullet lines that start with a kind tag and end with citations:

    - [fact] checkout's error ratio is `0.69`; payment's is `0`. [E1]

It fails (exit 1) when

- the ledger was changed after recording: an entry edited, removed or reordered, or an output
  file changed (hash chain and file hashes);
- a recorded command is not read-only by the allowlist;
- a fact, inference, root cause or ruled-out claim cites nothing (a hypothesis may cite nothing);
- a claim cites an id that is not in the ledger;
- a `quoted` value is not in any output the claim cites: text must appear verbatim (whitespace
  and JSON escaping aside), a number must match a recorded number after rounding to the digits
  quoted, and a percentage may stand for a ratio (`69%` for 0.6897);
- the report has no root-cause claim and no undetermined claim.

It warns, without failing, about claims that quote nothing (nothing in them was checked),
root causes that rest only on evidence that was added rather than run, and citations of
commands that exited with an error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import head, integrity_problems, read_ledger

KINDS = ("root-cause", "fact", "inference", "ruled-out", "hypothesis", "undetermined")
NEEDS_EVIDENCE = {"root-cause", "fact", "inference", "ruled-out"}
CLAIM = re.compile(r"^\s*[-*]\s+\[(?P<kind>[a-z-]+)\]\s+(?P<body>.+?)\s*$")
CITES = re.compile(r"\[(E\d+(?:\s*,\s*E\d+)*)\]\s*\.?\s*$")
QUOTE = re.compile(r"`([^`]+)`")
NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?%?$")
NUMBERS_IN_TEXT = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


@dataclass
class Claim:
    line: int
    kind: str
    text: str
    cites: list[str]
    quotes: list[str] = field(default_factory=list)


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    claims: int = 0
    quotes_checked: int = 0
    ledger_entries: int = 0
    ledger_head: str = ""


def parse_claims(report: str) -> tuple[list[Claim], list[str]]:
    claims, errors = [], []
    in_code = False
    for n, line in enumerate(report.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        m = None if in_code else CLAIM.match(line)
        if not m:
            continue
        kind, body = m["kind"], m["body"]
        if kind not in KINDS:
            errors.append(f"line {n}: unknown claim kind [{kind}]; use one of {', '.join(KINDS)}")
            continue
        c = CITES.search(body)
        cites = [x.strip() for x in c[1].split(",")] if c else []
        text = body[: c.start()].rstrip() if c else body
        claims.append(Claim(n, kind, text, cites, QUOTE.findall(text)))
    return claims, errors


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _texts(raw: str) -> list[str]:
    """The output as recorded, and as a human reads it when it is JSON (escapes undone)."""
    out = [_norm(raw)]
    unescaped = raw.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
    if unescaped != raw:
        out.append(_norm(unescaped))
    return out


def _round(x: Decimal, places: int) -> list[Decimal]:
    q = Decimal(1).scaleb(-places)
    return [x.quantize(q, rounding=ROUND_HALF_UP), x.quantize(q, rounding=ROUND_HALF_EVEN)]


def number_found(quote: str, raw: str) -> bool:
    pct = quote.endswith("%")
    q = Decimal(quote.rstrip("%").lstrip("+"))
    places = max(-q.as_tuple().exponent, 0)  # type: ignore[operator]
    targets = [(q, places)] + ([(q / 100, places + 2)] if pct else [])
    for m in NUMBERS_IN_TEXT.finditer(raw):
        try:
            v = Decimal(m.group())
        except InvalidOperation:
            continue
        for t, p in targets:
            if p == 0 and abs(v) < 1 and v != t:
                continue  # a whole number is not a rounded fraction: `1` does not stand for 0.69
            try:
                if t in _round(v, p):
                    return True
            except InvalidOperation:  # an id or timestamp too long to round at this precision
                continue
    return False


def quote_found(quote: str, raw: str) -> bool:
    if NUMBER.match(quote.strip()):
        return number_found(quote.strip(), raw)
    needle = _norm(quote)
    return any(needle in t for t in _texts(raw))


def verify(report: str, evdir: Path) -> Result:
    res = Result()
    entries = read_ledger(evdir)
    res.ledger_entries, res.ledger_head = len(entries), head(entries)
    if not entries:
        res.errors.append(f"no evidence ledger at {evdir}")
    res.errors += integrity_problems(evdir, entries)
    by_id = {e.id: e for e in entries}
    outputs: dict[str, str] = {}

    def output(ev_id: str) -> str:
        if ev_id not in outputs:
            f = evdir / str(by_id[ev_id].data.get("file", ""))
            outputs[ev_id] = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
        return outputs[ev_id]

    claims, parse_errors = parse_claims(report)
    res.errors += parse_errors
    res.claims = len(claims)
    if not claims:
        res.errors.append("no claims found: claim lines look like `- [fact] ... [E1]`")
    if not any(c.kind in ("root-cause", "undetermined") for c in claims):
        res.errors.append("no [root-cause] or [undetermined] claim: say what caused it, or that it is not known")

    for c in claims:
        where = f"line {c.line} [{c.kind}]"
        if c.kind in NEEDS_EVIDENCE and not c.cites:
            res.errors.append(f"{where}: cites no evidence (end the line with [E..], or make it a hypothesis)")
        missing = [x for x in c.cites if x not in by_id]
        for x in missing:
            res.errors.append(f"{where}: {x} is not in the ledger")
        cited = [x for x in c.cites if x in by_id]
        if c.kind == "hypothesis" or not cited:
            continue
        for q in c.quotes:
            res.quotes_checked += 1
            if not any(quote_found(q, output(x)) for x in cited):
                res.errors.append(f"{where}: `{q}` is not in {', '.join(cited)}")
        if not c.quotes and c.kind in NEEDS_EVIDENCE:
            res.warnings.append(f"{where}: quotes nothing, so nothing in it was checked against the evidence")
        if c.kind == "root-cause" and all(by_id[x].data.get("kind") != "run" for x in cited):
            res.warnings.append(f"{where}: rests only on added evidence, which was not run through ev.py")
        for x in cited:
            if by_id[x].data.get("kind") == "run" and by_id[x].data.get("exit") not in (0, None):
                res.warnings.append(f"{where}: cites {x}, whose command exited with {by_id[x].data.get('exit')}")
    return res


def find_evidence(report: Path) -> Path:
    for d in report.resolve().parents:
        if (d / ".evidence").is_dir():
            return d / ".evidence"
    return report.resolve().parent / ".evidence"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("report")
    p.add_argument("--dir", default=None, help="evidence directory (default: the nearest .evidence above the report)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    report = Path(a.report)
    evdir = Path(a.dir) if a.dir else find_evidence(report)
    res = verify(report.read_text(encoding="utf-8"), evdir)
    if a.json:
        print(json.dumps(res.__dict__, indent=2))
    else:
        for e in res.errors:
            print(f"FAIL  {e}")
        for w in res.warnings:
            print(f"warn  {w}")
        verdict = "FAILED" if res.errors else "passed"
        print(
            f"{verdict}: {res.claims} claims, {res.quotes_checked} quotes checked, "
            f"{len(res.errors)} errors, {len(res.warnings)} warnings; "
            f"{res.ledger_entries} evidence entries, ledger head {res.ledger_head}"
        )
    return 1 if res.errors else 0


if __name__ == "__main__":
    sys.exit(main())
