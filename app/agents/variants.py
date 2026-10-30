"""Ways to run the same investigation differently, for the evaluation's ablations (M7, ADR-0009).

A variant changes how an incident is investigated, never what the agents can do to the
system: the citation check, the planner and the policy stay the same, and the only tool a
variant may add is the read-only knowledge base. Production runs ``default``.
The lab runs the others side by side on the same faults: Alertmanager sends every alert to
one tenant per variant, and the worker looks the tenant up in ``SRE_EVAL_TENANT_VARIANTS``.

- ``default``: coordinator, three investigators in parallel, synthesizer.
- ``single``: one agent with every investigator's tools and their combined step budget,
  which writes the diagnosis itself. Is splitting the work worth it?
- ``strong-plan``: the coordinator and the synthesizer on a stronger model, the
  investigators unchanged. Do the two steps that only reason, and run once each, gain from it?
- ``rag`` (M6): the default, with the knowledge base. Every investigator also gets
  ``kb_search`` (runbooks and postmortems) and a prompt part on how to use it. Does
  background knowledge help, once the citation check keeps it from standing in for data?
- ``canary``: a synthesizer prompt with a plausible, wrong rule ("rank the alerting service
  first"): the classic event-host mistake. It exists to be caught. An evaluation that does
  not fail this variant cannot fail anything (prompts/canary_synthesizer.md).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from app.agents import prompts
from app.agents.prompts import Prompt

STRONG_MODEL = "claude-sonnet-5-5"


@dataclass(frozen=True)
class Variant:
    name: str
    mode: Literal["multi", "single"] = "multi"
    # Role -> model, over the configured routing (app/llm/router.py).
    models: Mapping[str, str] = field(default_factory=dict)
    # Role -> prompt, instead of the role's own.
    prompts: Mapping[str, Callable[[], Prompt]] = field(default_factory=dict)
    # The investigators (or the single agent) may search the knowledge base.
    knowledge: bool = False

    def prompt(self, role: str, default: Callable[[], Prompt]) -> Prompt:
        return self.prompts.get(role, default)()


DEFAULT = Variant("default")
VARIANTS: dict[str, Variant] = {
    v.name: v
    for v in (
        DEFAULT,
        Variant("single", mode="single"),
        Variant("strong-plan", models={"coordinator": STRONG_MODEL, "synthesizer": STRONG_MODEL}),
        Variant("rag", knowledge=True),
        Variant("canary", prompts={"synthesizer": prompts.canary_synthesizer}),
    )
}


def get(name: str) -> Variant:
    try:
        return VARIANTS[name]
    except KeyError:
        raise ValueError(f"unknown agent variant {name!r}; known: {', '.join(VARIANTS)}") from None


def for_tenant(tenant_id: str, tenant_variants: Mapping[str, str]) -> Variant:
    return get(tenant_variants.get(tenant_id, DEFAULT.name))
