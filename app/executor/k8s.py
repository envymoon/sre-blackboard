"""The executor's Kubernetes client and the actions of the catalogue (ADR-0007).

It authenticates as its own ServiceAccount (deploy/kind/manifests/executor.yaml): get, list
and patch Deployments, get and list ReplicaSets, in one namespace, nothing else. Every
change is a JSON patch that starts with a ``test`` of the resourceVersion read just before,
so a Deployment someone else changed in between is not overwritten (the API answers 422).
Each patch is first sent as a server-side dry run.
"""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.remediation.actions import Action, RestartDeployment, RolloutUndo, ScaleDeployment

REVISION = "deployment.kubernetes.io/revision"
CHANGE_CAUSE = "kubernetes.io/change-cause"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
JSON_PATCH = "application/json-patch+json"


class ExecError(Exception):
    """``applied`` says whether the cluster may have taken the change: None means nobody knows."""

    def __init__(self, message: str, *, applied: bool | None = False) -> None:
        super().__init__(message)
        self.applied = applied


@dataclass
class Prepared:
    """A change worked out against the Deployment as it is now."""

    deployment: str
    ops: list[dict[str, Any]]
    before: dict[str, Any]
    summary: str
    noop: bool = False  # already in the target state: nothing to send


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


class DeploymentWriter:
    def __init__(self, client: httpx.AsyncClient, namespace: str) -> None:
        self.client = client
        self.namespace = namespace

    @classmethod
    def from_settings(cls, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> DeploymentWriter:
        if settings.executor_k8s_token is None:
            raise RuntimeError("SRE_EXECUTOR_K8S_TOKEN is not set: the executor has no identity to act with")
        client = httpx.AsyncClient(
            base_url=settings.k8s_api_url,
            headers={"Authorization": f"Bearer {settings.executor_k8s_token.get_secret_value()}"},
            verify=settings.k8s_ca_file or True,
            timeout=30.0,
            transport=transport,
        )
        return cls(client, settings.k8s_namespace)

    async def aclose(self) -> None:
        await self.client.aclose()

    def _path(self, name: str) -> str:
        return f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}"

    async def _send(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self.client.request(method, path, **kw)
        except httpx.TransportError as e:
            # For a write, the request may have reached the API server before the connection broke.
            raise ExecError(
                f"no answer from the Kubernetes API: {e!r}", applied=None if method == "PATCH" else False
            ) from e
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message") or resp.text
            except ValueError:
                detail = resp.text
            raise ExecError(f"HTTP {resp.status_code} from {method} {path}: {str(detail)[:400]}")
        return resp.json()

    async def deployment(self, name: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._send("GET", self._path(name))
        return result

    async def replicasets(self, dep: dict[str, Any]) -> list[dict[str, Any]]:
        selector = ",".join(f"{k}={v}" for k, v in dep["spec"]["selector"].get("matchLabels", {}).items())
        body = await self._send(
            "GET", f"/apis/apps/v1/namespaces/{self.namespace}/replicasets", params={"labelSelector": selector}
        )
        uid = dep["metadata"]["uid"]
        return [
            r
            for r in body.get("items", [])
            if any(o.get("uid") == uid for o in r["metadata"].get("ownerReferences", []))
        ]

    async def patch(self, name: str, ops: list[dict[str, Any]], *, dry_run: bool) -> dict[str, Any]:
        params = {"dryRun": "All"} if dry_run else None
        result: dict[str, Any] = await self._send(
            "PATCH", self._path(name), json=ops, params=params, headers={"Content-Type": JSON_PATCH}
        )
        return result

    async def prepare(self, action: Action, *, note: str) -> Prepared:
        dep = await self.deployment(action.deployment)
        meta = dep["metadata"]
        before = {
            "resourceVersion": meta["resourceVersion"],
            "revision": int(meta.get("annotations", {}).get(REVISION, 0)),
            "replicas": dep["spec"].get("replicas"),
            "image": [c.get("image") for c in dep["spec"]["template"]["spec"].get("containers", [])],
        }
        guard = {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}
        if isinstance(action, RolloutUndo):
            return await self._undo(action, dep, before, guard, note)
        if isinstance(action, RestartDeployment):
            now = datetime.now(UTC).isoformat(timespec="seconds")
            annotations = dep["spec"]["template"]["metadata"].get("annotations")
            op = (
                {"op": "add", "path": f"/spec/template/metadata/annotations/{_escape(RESTARTED_AT)}", "value": now}
                if annotations is not None
                else {"op": "add", "path": "/spec/template/metadata/annotations", "value": {RESTARTED_AT: now}}
            )
            return Prepared(action.deployment, [guard, op], before, f"rolling restart of {action.deployment}")
        if isinstance(action, ScaleDeployment):
            if dep["spec"].get("replicas") == action.replicas:
                return Prepared(
                    action.deployment, [], before, f"{action.deployment} already has {action.replicas}", True
                )
            scale: dict[str, Any] = {"op": "replace", "path": "/spec/replicas", "value": action.replicas}
            summary = f"scale {action.deployment} from {before['replicas']} to {action.replicas} replicas"
            return Prepared(action.deployment, [guard, scale], before, summary)
        raise ExecError(f"no implementation for {action.action}")

    async def _undo(
        self, action: RolloutUndo, dep: dict[str, Any], before: dict[str, Any], guard: dict[str, Any], note: str
    ) -> Prepared:
        """What ``kubectl rollout undo`` does: copy an earlier ReplicaSet's pod template back."""
        current = before["revision"]
        by_revision = {
            int(r["metadata"].get("annotations", {}).get(REVISION, 0)): r for r in await self.replicasets(dep)
        }
        if action.to_revision is not None:
            target = action.to_revision
        else:
            earlier = [r for r in by_revision if 0 < r < current]
            if not earlier:
                raise ExecError(f"{action.deployment} has no revision before {current} to roll back to")
            target = max(earlier)
        if target not in by_revision:
            raise ExecError(f"{action.deployment} has no revision {target} (known: {sorted(by_revision)})")
        if target == current:
            raise ExecError(f"{action.deployment} is already at revision {target}")
        template = copy.deepcopy(by_revision[target]["spec"]["template"])
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        if _same_template(template, dep["spec"]["template"]):
            summary = f"{action.deployment} already runs the template of revision {target}"
            return Prepared(action.deployment, [], before, summary, noop=True)
        ops = [
            guard,
            {"op": "replace", "path": "/spec/template", "value": template},
            {"op": "add", "path": f"/metadata/annotations/{_escape(CHANGE_CAUSE)}", "value": note},
        ]
        summary = f"roll {action.deployment} back from revision {current} to the template of revision {target}"
        return Prepared(action.deployment, ops, {**before, "target_revision": target}, summary)

    async def wait_rolled_out(self, name: str, generation: int, timeout_s: float) -> dict[str, Any]:
        """Until the controller has rolled the change out (``kubectl rollout status``), or the timeout."""
        deadline = time.monotonic() + timeout_s
        while True:
            dep = await self.deployment(name)
            st, want = dep.get("status", {}), dep["spec"].get("replicas", 1)
            done = (
                st.get("observedGeneration", 0) >= generation
                and st.get("updatedReplicas", 0) == want
                and st.get("availableReplicas", 0) == want
                and st.get("replicas", 0) == want
            )
            state = {
                "rolled_out": done,
                "revision": int(dep["metadata"].get("annotations", {}).get(REVISION, 0)),
                "replicas": want,
                "available": st.get("availableReplicas", 0),
                "resourceVersion": dep["metadata"]["resourceVersion"],
            }
            if done or time.monotonic() >= deadline:
                return state
            await asyncio.sleep(min(2.0, timeout_s / 5))


def _same_template(a: dict[str, Any], b: dict[str, Any]) -> bool:
    def clean(t: dict[str, Any]) -> dict[str, Any]:
        t = copy.deepcopy(t)
        t.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        t.get("metadata", {}).get("annotations", {}).pop(RESTARTED_AT, None)
        return t

    return clean(a) == clean(b)
