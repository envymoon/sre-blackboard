"""Print lab output files as GitHub Actions notice annotations.

Job logs and artifacts live in blob storage that some environments (such as the cloud dev
container) cannot reach, but check-run annotations are part of the GitHub REST API. This
puts the important lab output where any API client can read it:

    gh api repos/OWNER/REPO/check-runs/JOB_ID/annotations

Usage: python -m scripts.lab.annotate FILE[:TAIL_LINES] ...  (files in priority order)
GitHub keeps at most 10 notices per step (and 50 annotations per job) and silently drops the
rest, so output is cut at MAX_ANNOTATIONS; spread larger output over several steps.
"""

from __future__ import annotations

import sys
from pathlib import Path

CHUNK = 6000
MAX_ANNOTATIONS = 10


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def build(specs: list[str], chunk: int = CHUNK, limit: int = MAX_ANNOTATIONS) -> list[str]:
    out: list[str] = []
    for spec in specs:
        path, _, tail = spec.partition(":")
        file = Path(path)
        if not file.is_file():
            continue
        lines = file.read_text(errors="replace").splitlines()
        if tail:
            lines = lines[-int(tail) :]
        text = "\n".join(lines).strip() or "(empty)"
        parts = [text[i : i + chunk] for i in range(0, len(text), chunk)]
        for n, part in enumerate(parts, 1):
            if len(out) >= limit:
                return out
            out.append(f"::notice title={file.name} [{n}/{len(parts)}]::{_escape(part)}")
    return out


if __name__ == "__main__":
    print("\n".join(build(sys.argv[1:])))
