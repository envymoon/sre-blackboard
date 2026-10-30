"""Versioned role prompts (app/agents/prompts/*.md).

Each file starts with ``version: N``. A prompt's version is the versions of the files it is
made of plus a short hash of their text, e.g. ``investigator.metrics@1.1.1-3f2a9c01``: bumping
the number is a statement ("this changed on purpose"), the hash catches an edit nobody
bumped. The version is stored with every agent run and LLM call, and is part of the replay key.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from app.tools.base import sha256_hex

PROMPTS = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    version: str


@cache
def _part(name: str) -> tuple[str, str]:
    raw = (PROMPTS / f"{name}.md").read_text()
    first, _, body = raw.partition("\n")
    if not first.startswith("version: "):
        raise ValueError(f"prompts/{name}.md must start with 'version: N'")
    return first.removeprefix("version: ").strip(), body.strip()


def compose(name: str, *parts: str) -> Prompt:
    loaded = [_part(p) for p in parts]
    text = "\n\n".join(body for _, body in loaded)
    numbers = ".".join(v for v, _ in loaded)
    return Prompt(name, text, f"{name}@{numbers}-{sha256_hex(text)[:8]}")


def coordinator() -> Prompt:
    return compose("coordinator", "coordinator", "environment")


def investigator(focus: str, knowledge: bool = False) -> Prompt:
    parts = ("investigator", focus, *(("knowledge",) if knowledge else ()), "environment")
    return compose(f"investigator.{focus}", *parts)


def synthesizer() -> Prompt:
    return compose("synthesizer", "synthesizer", "environment")


def solo(knowledge: bool = False) -> Prompt:
    """The single-agent variant (app/agents/variants.py): every investigator's brief, and the synthesizer's job."""
    parts = ("solo", "metrics", "logs_traces", "changes", *(("knowledge",) if knowledge else ()), "environment")
    return compose("solo", *parts)


def canary_synthesizer() -> Prompt:
    """The canary variant's synthesizer: deliberately wrong, for the evaluation to catch."""
    return compose("synthesizer", "canary_synthesizer", "environment")


def planner() -> Prompt:
    return compose("planner", "planner", "environment")
