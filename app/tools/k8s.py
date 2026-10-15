"""k8s_events, rollout_history and deploy_diff: the Kubernetes API, read-only.

Authenticated as a ServiceAccount that may only get/list events, deployments, replicasets
and pods in the demo namespace (deploy/kind/manifests/reader.yaml).

Events from Chaos Mesh objects are dropped at fetch time. They describe how the lab injects a
fault ("Successfully apply chaos for otel-demo/product-catalog"), which no production system
would have; showing them would let the agent read the answer instead of diagnosing it.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from pydantic import Field

from app.tools.base import Tool, ToolArgs, ToolError
from app.tools.clients import Clients, get_json
from app.tools.compress import clip

TOP = 15
HIDDEN_API_GROUPS = ("chaos-mesh.org",)
REVISION = "deployment.kubernetes.io/revision"
CHANGE_CAUSE = "kubernetes.io/change-cause"
_NAME = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


class EventsArgs(ToolArgs):
    object_name: str | None = Field(
        None, pattern=_NAME, max_length=253, description="Only objects whose name starts with this (e.g. checkout)"
    )
    warnings_only: bool = False
    minutes: int = Field(60, ge=1, le=1440)


class DeploymentArgs(ToolArgs):
    deployment: str = Field(pattern=_NAME, max_length=253)


class DeployDiffArgs(ToolArgs):
    deployment: str = Field(pattern=_NAME, max_length=253)
    from_revision: int | None = Field(None, ge=1, description="Default: the revision before to_revision")
    to_revision: int | None = Field(None, ge=1, description="Default: the current revision")


def _strip(obj: dict[str, Any]) -> dict[str, Any]:
    meta = {k: v for k, v in obj.get("metadata", {}).items() if k != "managedFields"}
    return {**obj, "metadata": meta}


def _hidden(event: dict[str, Any]) -> bool:
    api = event.get("involvedObject", {}).get("apiVersion", "")
    return any(api.startswith(group) for group in HIDDEN_API_GROUPS)


def _event_time(e: dict[str, Any]) -> str:
    return str(e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp") or "")


async def _events(c: Clients, args: EventsArgs, now: datetime) -> Any:
    body = await get_json(c.k8s, f"/api/v1/namespaces/{c.namespace}/events")
    since = now - timedelta(minutes=args.minutes)
    out = []
    for e in body.get("items", []):
        if _hidden(e):
            continue
        when = _event_time(e)
        if when and datetime.fromisoformat(when.replace("Z", "+00:00")) < since:
            continue
        if args.object_name and not e.get("involvedObject", {}).get("name", "").startswith(args.object_name):
            continue
        if args.warnings_only and e.get("type") != "Warning":
            continue
        out.append(_strip(e))
    return {"namespace": c.namespace, "items": out}


async def _deployment(c: Clients, args: DeploymentArgs | DeployDiffArgs, now: datetime) -> Any:
    ns = c.namespace
    dep = await get_json(c.k8s, f"/apis/apps/v1/namespaces/{ns}/deployments/{args.deployment}")
    selector = ",".join(f"{k}={v}" for k, v in dep["spec"]["selector"].get("matchLabels", {}).items())
    rs = await get_json(c.k8s, f"/apis/apps/v1/namespaces/{ns}/replicasets", params={"labelSelector": selector})
    uid = dep["metadata"]["uid"]
    owned = [
        _strip(r)
        for r in rs.get("items", [])
        if any(o.get("uid") == uid for o in r["metadata"].get("ownerReferences", []))
    ]
    return {"namespace": ns, "deployment": _strip(dep), "replicasets": owned}


def summarize_events(raw: dict[str, Any], args: EventsArgs) -> dict[str, Any]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for e in raw["items"]:
        obj = e.get("involvedObject", {})
        key = (e.get("type", ""), e.get("reason", ""), obj.get("kind", ""), obj.get("name", ""))
        g = groups.setdefault(
            key,
            {
                "type": key[0],
                "reason": key[1],
                "object": f"{key[2]}/{key[3]}",
                "count": 0,
                "last_seen": "",
                "message": "",
            },
        )
        g["count"] += int(e.get("count") or 1)
        when = _event_time(e)
        if when >= g["last_seen"]:
            g["last_seen"] = when
            g["message"] = clip(e.get("message", ""), 300)
    # Warnings first, newest first within each type.
    newest_first = sorted(groups.values(), key=lambda g: g["last_seen"], reverse=True)
    ordered = [g for g in newest_first if g["type"] == "Warning"] + [g for g in newest_first if g["type"] != "Warning"]
    by_reason = Counter(g["reason"] for g in groups.values())
    out: dict[str, Any] = {
        "namespace": raw["namespace"],
        "events": sum(g["count"] for g in groups.values()),
        "warnings": sum(g["count"] for g in groups.values() if g["type"] == "Warning"),
        "by_reason": dict(by_reason.most_common(TOP)),
        "groups": ordered[:TOP],
    }
    if not groups:
        out["note"] = "no events in this window"
    return out


def _revision(rs: dict[str, Any]) -> int:
    return int(rs["metadata"].get("annotations", {}).get(REVISION, 0))


def _containers(rs: dict[str, Any]) -> list[dict[str, Any]]:
    spec = rs["spec"]["template"]["spec"]
    return list(spec.get("initContainers", [])) + list(spec.get("containers", []))


def _revisions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(raw["replicasets"], key=_revision, reverse=True)


def summarize_history(raw: dict[str, Any], args: DeploymentArgs) -> dict[str, Any]:
    dep = raw["deployment"]
    status = dep.get("status", {})
    revisions = _revisions(raw)
    current = _revision(revisions[0]) if revisions else None
    return {
        "deployment": dep["metadata"]["name"],
        "change_cause": dep["metadata"].get("annotations", {}).get(CHANGE_CAUSE),
        "generation": dep["metadata"].get("generation"),
        "replicas": {
            k: status.get(k, 0) for k in ("replicas", "readyReplicas", "updatedReplicas", "unavailableReplicas")
        },
        "conditions": [
            {k: clip(str(c.get(k, "")), 200) for k in ("type", "status", "reason", "message")}
            for c in status.get("conditions", [])
        ],
        "revisions": [
            {
                "revision": _revision(rs),
                "current": _revision(rs) == current,
                "created": rs["metadata"].get("creationTimestamp"),
                "change_cause": rs["metadata"].get("annotations", {}).get(CHANGE_CAUSE),
                "replicas": rs.get("status", {}).get("replicas", 0),
                "images": sorted({c["image"] for c in _containers(rs)}),
            }
            for rs in revisions[:10]
        ],
    }


def _flatten(rs: dict[str, Any]) -> dict[str, str]:
    """The parts of a pod template that a release changes, as path -> value."""
    template = rs["spec"]["template"]
    out: dict[str, str] = {}
    for k, v in sorted(template["metadata"].get("annotations", {}).items()):
        out[f"annotations.{k}"] = str(v)
    for c in _containers(rs):
        prefix = f"containers.{c['name']}"
        out[f"{prefix}.image"] = c.get("image", "")
        for field in ("command", "args", "resources", "ports"):
            if field in c:
                out[f"{prefix}.{field}"] = str(c[field])
        for env in c.get("env", []):
            value = env["value"] if "value" in env else f"<from {sorted(env.get('valueFrom', {}))}>"
            out[f"{prefix}.env.{env['name']}"] = str(value)
    return out


def summarize_diff(raw: dict[str, Any], args: DeployDiffArgs) -> dict[str, Any]:
    revisions = {_revision(rs): rs for rs in raw["replicasets"]}
    if not revisions:
        raise ToolError("not_found", f"deployment {args.deployment} has no revisions")
    ordered = sorted(revisions)
    to_rev = args.to_revision or ordered[-1]
    older = [r for r in ordered if r < to_rev]
    from_rev = args.from_revision or (older[-1] if older else None)
    if to_rev not in revisions or from_rev not in revisions:
        raise ToolError("not_found", f"revisions available: {ordered}; asked for {from_rev} -> {to_rev}")
    a, b = _flatten(revisions[from_rev]), _flatten(revisions[to_rev])
    changes = [
        {"path": path, "from": clip(a.get(path, "<absent>"), 200), "to": clip(b.get(path, "<absent>"), 200)}
        for path in sorted(set(a) | set(b))
        if a.get(path) != b.get(path)
    ]
    meta = {r: revisions[r]["metadata"] for r in (from_rev, to_rev)}
    out = {
        "deployment": args.deployment,
        "from": {
            "revision": from_rev,
            "created": meta[from_rev].get("creationTimestamp"),
            "change_cause": meta[from_rev].get("annotations", {}).get(CHANGE_CAUSE),
        },
        "to": {
            "revision": to_rev,
            "created": meta[to_rev].get("creationTimestamp"),
            "change_cause": meta[to_rev].get("annotations", {}).get(CHANGE_CAUSE),
        },
        "changes": changes[:30],
    }
    if not changes:
        out["note"] = "no differences in the pod template"
    return out


K8S_EVENTS = Tool(
    name="k8s_events",
    description=(
        "Kubernetes events in the application namespace: warnings first, grouped by reason and object "
        "(restarts, failed probes, OOM kills, scheduling, rollouts)."
    ),
    args=EventsArgs,
    backend="k8s",
    fetch=_events,
    summarize=summarize_events,
)

ROLLOUT_HISTORY = Tool(
    name="rollout_history",
    description=(
        "A Deployment's rollout state and revision history: each revision's creation time, change cause and "
        "images. Use it to see whether something was released just before the symptoms started."
    ),
    args=DeploymentArgs,
    backend="k8s",
    fetch=_deployment,
    summarize=summarize_history,
)

DEPLOY_DIFF = Tool(
    name="deploy_diff",
    description=(
        "What changed in a Deployment's pod template between two revisions (default: the current one and the "
        "one before): images, environment variables, arguments, resources."
    ),
    args=DeployDiffArgs,
    backend="k8s",
    fetch=_deployment,
    summarize=summarize_diff,
)
