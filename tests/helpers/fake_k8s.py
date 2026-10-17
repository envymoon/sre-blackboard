"""A Kubernetes API stand-in for executor tests, seeded with the lab's real checkout Deployment
and its two ReplicaSets (revision 1: payment:8080, revision 2: the bad payment:9999).

It does what the executor relies on: GET a Deployment and its ReplicaSets, and PATCH with a
JSON patch (test / add / replace), dry run or not. A real patch that changes the pod template
acts like the Deployment controller: the ReplicaSet with that template becomes the newest
revision and the rollout completes at once. Failure modes are switches.
"""

from __future__ import annotations

import copy
import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FIXTURE = Path(__file__).parent.parent / "fixtures" / "lab" / "bad-release.jsonl.gz"
REVISION = "deployment.kubernetes.io/revision"
TOKEN = "executor-token"


def _recorded() -> dict[str, Any]:
    with gzip.open(FIXTURE, "rt") as f:
        for line in f:
            entry = json.loads(line)
            if entry.get("tool") == "rollout_history":
                raw: dict[str, Any] = entry["raw"]
                return raw
    raise LookupError("no rollout_history entry in the bad-release recording")


def _pointer(path: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~") for p in path.lstrip("/").split("/")]


def _apply(doc: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    for op in ops:
        *parents, last = _pointer(op["path"])
        node: Any = out
        for p in parents:
            node = node[p]
        if op["op"] == "test":
            if node.get(last) != op["value"]:
                raise ValueError(f"test failed at {op['path']}")
        elif op["op"] in ("add", "replace"):
            if op["op"] == "replace" and last not in node:
                raise ValueError(f"replace of missing {op['path']}")
            node[last] = copy.deepcopy(op["value"])
        else:
            raise ValueError(f"unsupported op {op['op']}")
    return out


def _template_key(template: dict[str, Any]) -> str:
    t = copy.deepcopy(template)
    t.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
    return json.dumps(t, sort_keys=True)


@dataclass
class FakeCluster:
    namespace: str = "otel-demo"
    deployment: dict[str, Any] = field(default_factory=dict)
    replicasets: list[dict[str, Any]] = field(default_factory=list)
    patches: list[tuple[bool, list[dict[str, Any]]]] = field(default_factory=list)  # (dry_run, ops)
    # Switches:
    timeout_on_real_patch: bool = False  # the connection breaks: did it land? nobody knows
    changed_by_someone_else: bool = False  # resourceVersion moves right before the real patch
    rollout_stuck: bool = False

    @classmethod
    def checkout(cls) -> FakeCluster:
        raw = _recorded()
        return cls(deployment=copy.deepcopy(raw["deployment"]), replicasets=copy.deepcopy(raw["replicasets"]))

    def env(self, name: str) -> str | None:
        for c in self.deployment["spec"]["template"]["spec"]["containers"]:
            for e in c.get("env", []):
                if e["name"] == name:
                    return str(e.get("value"))
        return None

    @property
    def revision(self) -> int:
        return int(self.deployment["metadata"]["annotations"][REVISION])

    def real_patches(self) -> int:
        return sum(1 for dry, _ in self.patches if not dry)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(403, json={"message": "forbidden"})
        base = f"/apis/apps/v1/namespaces/{self.namespace}"
        name = self.deployment["metadata"]["name"]
        path = request.url.path
        if request.method == "GET" and path == f"{base}/deployments/{name}":
            return httpx.Response(200, json=self.deployment)
        if request.method == "GET" and path == f"{base}/replicasets":
            return httpx.Response(200, json={"items": self.replicasets})
        if request.method == "PATCH" and path == f"{base}/deployments/{name}":
            if request.headers.get("Content-Type") != "application/json-patch+json":
                return httpx.Response(415, json={"message": "unsupported media type"})
            dry = request.url.params.get("dryRun") == "All"
            ops = json.loads(request.content)
            self.patches.append((dry, ops))
            if not dry and self.changed_by_someone_else:
                self._bump()
            if not dry and self.timeout_on_real_patch:
                raise httpx.ReadTimeout("the API server did not answer", request=request)
            try:
                patched = _apply(self.deployment, ops)
            except (ValueError, KeyError) as e:
                return httpx.Response(422, json={"message": f"the server rejected the patch: {e}"})
            if dry:
                return httpx.Response(200, json=patched)
            self._commit(patched)
            return httpx.Response(200, json=self.deployment)
        return httpx.Response(404, json={"message": f"{request.method} {path} not found"})

    def _bump(self) -> None:
        meta = self.deployment["metadata"]
        meta["resourceVersion"] = str(int(meta["resourceVersion"]) + 1)

    def _commit(self, patched: dict[str, Any]) -> None:
        template_changed = _template_key(patched["spec"]["template"]) != _template_key(
            self.deployment["spec"]["template"]
        )
        self.deployment = patched
        meta = self.deployment["metadata"]
        self._bump()
        meta["generation"] = int(meta["generation"]) + 1
        if template_changed:
            # The controller: the ReplicaSet with this template becomes the newest revision.
            newest = max(int(r["metadata"]["annotations"][REVISION]) for r in self.replicasets) + 1
            key = _template_key(patched["spec"]["template"])
            match = next((r for r in self.replicasets if _template_key(r["spec"]["template"]) == key), None)
            if match is None:
                match = copy.deepcopy(self.replicasets[0])
                match["spec"]["template"] = copy.deepcopy(patched["spec"]["template"])
                self.replicasets.append(match)
            match["metadata"]["annotations"][REVISION] = str(newest)
            meta["annotations"][REVISION] = str(newest)
        status = self.deployment.setdefault("status", {})
        replicas = self.deployment["spec"]["replicas"]
        if not self.rollout_stuck:
            status.update(
                observedGeneration=meta["generation"],
                replicas=replicas,
                updatedReplicas=replicas,
                availableReplicas=replicas,
            )


@dataclass
class FakePrometheus:
    """Answers every instant query with the next value of a script (the last one repeats)."""

    values: list[float | None]
    queries: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.queries.append(request.url.params["query"])
            value = self.values[min(len(self.queries), len(self.values)) - 1]
            result = [] if value is None else [{"metric": {}, "value": [1_760_500_000, str(value)]}]
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})

        return httpx.MockTransport(handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://prom.test", transport=self.transport())
