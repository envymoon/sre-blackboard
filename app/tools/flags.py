"""flag_state: the demo's feature flags, read from flagd's configuration (through flagd-ui).

A flag turned on is a change, like a release: the change-and-config investigator checks it the
same way it checks rollout history. Every flag in the demo defaults to the "off" variant.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.tools.base import Tool, ToolArgs, ToolError
from app.tools.clients import Clients, get_json


class FlagStateArgs(ToolArgs):
    flag: str | None = Field(None, pattern=r"^[A-Za-z0-9_.-]{1,100}$", description="One flag; default: all")


async def _read(c: Clients, args: FlagStateArgs, now: datetime) -> Any:
    body = await get_json(c.flagd, "/api/read")
    flags = body.get("flags")
    if not isinstance(flags, dict):
        raise ToolError("backend_error", "flag configuration has no 'flags' object")
    return {"flags": flags}


def _is_on(flag: dict[str, Any]) -> bool:
    return flag.get("state", "ENABLED") == "ENABLED" and flag.get("defaultVariant", "off") != "off"


def summarize(raw: dict[str, Any], args: FlagStateArgs) -> dict[str, Any]:
    flags: dict[str, Any] = raw["flags"]
    on = [
        {
            "flag": name,
            "variant": f["defaultVariant"],
            "value": f.get("variants", {}).get(f["defaultVariant"]),
            "targeting": bool(f.get("targeting")),
        }
        for name, f in sorted(flags.items())
        if _is_on(f)
    ]
    out: dict[str, Any] = {"flags": len(flags), "on": on}
    if args.flag:
        f = flags.get(args.flag)
        if f is None:
            raise ToolError("not_found", f"no flag {args.flag!r}; flags: {', '.join(sorted(flags))}")
        out["flag"] = {
            "name": args.flag,
            "on": _is_on(f),
            "state": f.get("state"),
            "variant": f.get("defaultVariant"),
            "variants": f.get("variants"),
        }
    if not on:
        out["note"] = "every flag is at its default (off)"
    return out


FLAG_STATE = Tool(
    name="flag_state",
    description="Feature flags that are switched on (flagd), or one flag's full state. A flag turned on is a change.",
    args=FlagStateArgs,
    backend="flagd",
    fetch=_read,
    summarize=summarize,
)
