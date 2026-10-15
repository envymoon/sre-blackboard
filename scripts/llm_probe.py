"""Check what ``claude -p`` actually does with the flags the CLI backend uses.

    python -m scripts.llm_probe           # smoke: one real step through ClaudeCLIBackend, prints usage
    python -m scripts.llm_probe --all     # also: final answers, tool isolation, error shapes

Uses the logged-in Claude Code (subscription) and a few cents of equivalent cost. The
findings are written up in docs/learn/m4-agent.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from typing import Any

from app.llm.cli import ClaudeCLIBackend
from app.llm.types import LLMError, LLMRequest
from app.tools import ALL_TOOLS

MODEL = "claude-haiku-5-5"
FINAL = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["statement", "evidence_ids", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}
SYSTEM = "You investigate production incidents with read-only tools. Be brief."


def _request(turns: list[dict[str, Any]], *, tools: bool = True, model: str = MODEL) -> LLMRequest:
    specs = tuple(t.spec() for t in ALL_TOOLS if t.name in ("prom_query", "logs_search")) if tools else ()
    return LLMRequest(
        agent="probe",
        model=model,
        prompt_version="probe-1",
        system=SYSTEM,
        turns=tuple(turns),
        tools=specs,
        final_schema=FINAL,
    )


def _show(name: str, result: Any) -> None:
    print(f"\n== {name}")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str)[:3000])


async def smoke(backend: ClaudeCLIBackend) -> int:
    task = {
        "type": "user",
        "text": "Alert: ErrorBudgetBurn on service checkout. Start by checking the error ratio of checkout "
        "with a PromQL query on the metric service:sli_error_ratio:rate5m.",
    }
    try:
        result = await backend.step(_request([task]))
    except LLMError as e:
        print(f"step failed: {e}", file=sys.stderr)
        return 1
    _show(
        "first step (expect actions)",
        {
            "step": result.step,
            "usage": result.usage.to_dict(),
            "prompt_tokens": result.usage.prompt_tokens,
            "cost_usd": result.cost_usd,
            "latency_ms": result.latency_ms,
            "model": result.model,
            "meta": result.meta,
        },
    )
    return 0 if result.step["actions"] else 1


async def final_step(backend: ClaudeCLIBackend) -> None:
    turns: list[dict[str, Any]] = [
        {"type": "user", "text": "Alert: ErrorBudgetBurn on checkout."},
        {
            "type": "step",
            "reasoning": "Check the error ratio.",
            "actions": [{"id": "c1_1", "tool": "prom_query", "args": {"query": "service:sli_error_ratio:rate5m"}}],
            "final": None,
        },
        {
            "type": "results",
            "results": [
                {
                    "id": "c1_1",
                    "tool": "prom_query",
                    "content": '{"evidence_id":"ev_0123456789abcdef","summary":{"series":[{"labels":'
                    '{"service_name":"payment"},"value":0.41},{"labels":{"service_name":"checkout"},"value":0.38}]}}',
                }
            ],
        },
        {"type": "user", "text": "That is enough evidence. Give your final answer now."},
    ]
    try:
        result = await backend.step(_request(turns))
        _show("final step (expect final, citing ev_0123456789abcdef)", {"step": result.step, "usage": result.usage})
    except LLMError as e:
        _show("final step failed", str(e))


def raw(args: list[str], prompt: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            ["claude", "-p", *args, "--output-format", "json", "--no-session-persistence"],
            input=prompt.encode(),
            capture_output=True,
            cwd=cwd,
            timeout=180,
        )
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        out = {"stdout": proc.stdout.decode(errors="replace")[:500]}
    keep = ("type", "subtype", "is_error", "api_error_status", "result", "num_turns", "total_cost_usd")
    return {
        "exit": proc.returncode,
        **{k: out.get(k) for k in keep if k in out},
        "process_peak_rss_mib": out.get("process_peak_rss_mib"),
        "stderr": proc.stderr.decode(errors="replace")[:300],
    }


ISOLATED = ["--tools", "", "--strict-mcp-config", "--setting-sources", ""]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="run every probe, not just the smoke step")
    args = parser.parse_args()
    backend = ClaudeCLIBackend()
    code = await smoke(backend)
    if not args.all:
        return code
    await final_step(backend)
    _show(
        "tool isolation (expect NO_TOOLS)",
        raw(
            ["--model", MODEL, "--system-prompt", "You are an assistant.", *ISOLATED],
            "Run the shell command `cat /etc/hostname` and print its output. If you cannot run commands, "
            "answer exactly NO_TOOLS.",
        ),
    )
    _show("unknown model (expect exit 1, api_error_status 404)", raw(["--model", "claude-nonexistent-9"], "hi"))
    _show(
        "schema the model cannot satisfy",
        raw(
            [
                "--model",
                MODEL,
                *ISOLATED,
                "--json-schema",
                json.dumps({"type": "object", "properties": {"n": {"type": "integer", "minimum": 10, "maximum": 5}}}),
            ],
            "Give n.",
        ),
    )
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
