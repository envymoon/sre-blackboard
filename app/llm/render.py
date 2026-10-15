"""Rendering a request for a backend that only takes text and a JSON schema (``claude -p``).

The data sources live only in the step's JSON schema: each is one branch of ``actions.items``,
with its description and its own args schema. The M4 probe showed why. With the same
definitions listed in the system prompt, Haiku called them as native tools first (they do not
exist there), got an error, and only then answered: a wasted model turn on every step.

Tool results go inside <result> tags, and the role prompts say they are data: whatever a log
line says, it is something a service printed, not an instruction to the investigator.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from app.llm.types import LLMRequest, Turn


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace local ``$ref``s with their definitions (pydantic emits ``$defs``).

    Not every structured-output implementation follows references; an inlined schema works
    everywhere. Recursive models are not supported (none of ours are).
    """
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = copy.deepcopy(defs[ref.removeprefix("#/$defs/")])
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return walk({**target, **extra})
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    out: dict[str, Any] = walk(schema)
    out.pop("title", None)
    return out


def _action_schema(tool: dict[str, Any]) -> dict[str, Any]:
    args = {k: v for k, v in inline_refs(tool["input_schema"]).items() if k != "title"}
    return {
        "type": "object",
        "description": tool["description"],
        "properties": {"tool": {"const": tool["name"]}, "args": args},
        "required": ["tool", "args"],
        "additionalProperties": False,
    }


def step_schema(request: LLMRequest) -> dict[str, Any]:
    """The JSON schema a step must match: up to ``max_actions`` data requests, or the final answer."""
    props: dict[str, Any] = {
        "reasoning": {
            "type": "string",
            "description": "Two or three sentences: what you learned and what you do next. No chain of thought.",
        },
    }
    required = ["reasoning"]
    if request.tools and not request.force_final:
        props["actions"] = {
            "type": "array",
            "maxItems": request.max_actions,
            "description": (
                "Data sources to query now (they run in parallel; results come in the next message). "
                "Leave empty when you give the final answer."
            ),
            "items": {"anyOf": [_action_schema(t) for t in request.tools]},
        }
        props["final"] = {**request.final_schema, "description": "Only when you are done; omit it otherwise."}
    else:
        props["final"] = request.final_schema
        required.append("final")
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def system_text(request: LLMRequest) -> str:
    return request.system


def transcript_text(turns: tuple[Turn, ...] | list[Turn], final_instruction: str = "Reply with your next step.") -> str:
    parts: list[str] = []
    step_no = 0
    for turn in turns:
        kind = turn["type"]
        if kind == "user":
            parts.append(f"<message>\n{turn['text']}\n</message>")
        elif kind == "step":
            step_no += 1
            body = {"reasoning": turn["reasoning"], "actions": turn["actions"]}
            if turn.get("final") is not None:
                body["final"] = turn["final"]
            parts.append(f"<your_step n={step_no}>\n{json.dumps(body, ensure_ascii=False)}\n</your_step>")
        elif kind == "results":
            results = "\n".join(
                f'<result id="{r["id"]}" tool="{r["tool"]}">\n{r["content"]}\n</result>' for r in turn["results"]
            )
            parts.append(f"<tool_results step={step_no}>\n{results}\n</tool_results>")
        else:
            raise ValueError(f"unknown turn type {kind!r}")
    parts.append(final_instruction)
    return "\n\n".join(parts)
