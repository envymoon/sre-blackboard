"""Injecting and undoing faults in the running lab (kubectl, flagd-ui and Prometheus)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from scripts.lab.faults import (
    CATALOGUE,
    CHANGE_CAUSE,
    NAMESPACE,
    ChaosFault,
    Fault,
    FlagFault,
    ReleaseFault,
    chaos_manifest,
    release_patch,
    with_flag,
)

FLAGD_SCHEMA = "https://flagd.dev/schema/v0/flags.json"
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class Kubectl:
    context: str = "kind-sre-lab"
    namespace: str = NAMESPACE
    run: Runner = subprocess.run

    def __call__(self, *args: str, stdin: str | None = None, check: bool = True) -> str:
        cmd = ["kubectl", "--context", self.context, "-n", self.namespace, *args]
        result = self.run(cmd, input=stdin, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
        return result.stdout


class Lab:
    """One lab cluster. ``base_url`` is the demo's frontend-proxy (flagd-ui lives at /feature)."""

    def __init__(
        self,
        kubectl: Kubectl | None = None,
        base_url: str = "http://localhost:18080",
        prometheus_url: str = "http://localhost:19090",
        http: httpx.Client | None = None,
    ) -> None:
        self.kubectl = kubectl or Kubectl()
        self.base_url = base_url.rstrip("/")
        self.prometheus_url = prometheus_url.rstrip("/")
        self.http = http or httpx.Client(timeout=15)

    # --- flags ------------------------------------------------------------------------------
    def flags(self) -> dict[str, Any]:
        resp = self.http.get(f"{self.base_url}/feature/api/read")
        resp.raise_for_status()
        flags: dict[str, Any] = resp.json()["flags"]
        return flags

    def set_flag(self, flag: str, variant: str) -> None:
        updated = with_flag(self.flags(), flag, variant)
        resp = self.http.post(
            f"{self.base_url}/feature/api/write", json={"data": {"$schema": FLAGD_SCHEMA, "flags": updated}}
        )
        resp.raise_for_status()
        # flagd-ui writes the file asynchronously; read back to make sure the change landed.
        current = self.flags()[flag]["defaultVariant"]
        if current != variant:
            raise RuntimeError(f"flag {flag} is {current!r} after writing {variant!r}")

    # --- inject / recover -------------------------------------------------------------------
    def inject(self, fault: Fault) -> None:
        m = fault.mechanism
        if isinstance(m, FlagFault):
            self.set_flag(m.flag, m.variant)
        elif isinstance(m, ChaosFault):
            self.kubectl("apply", "-f", "-", stdin=json.dumps(chaos_manifest(fault)))
        elif isinstance(m, ReleaseFault):
            self.kubectl(
                "patch", "deployment", m.deployment, "--type=strategic", "-p", json.dumps(release_patch(fault))
            )
            self.kubectl("rollout", "status", f"deployment/{m.deployment}", "--timeout=180s")

    def recover(self, fault: Fault) -> bool:
        """Undo a fault if it is active. Returns whether anything was changed."""
        if not self.is_active(fault):
            return False
        m = fault.mechanism
        if isinstance(m, FlagFault):
            self.set_flag(m.flag, "off")
        elif isinstance(m, ChaosFault):
            self.kubectl("delete", m.kind.lower(), fault.chaos_name(), "--ignore-not-found", "--wait=true")
        elif isinstance(m, ReleaseFault):
            self.kubectl("rollout", "undo", f"deployment/{m.deployment}")
            # rollout undo restores the pod template but not the Deployment's own annotation.
            self.kubectl(
                "annotate", "deployment", m.deployment, f"{CHANGE_CAUSE}=rollback of: {m.change_cause}", "--overwrite"
            )
            self.kubectl("rollout", "status", f"deployment/{m.deployment}", "--timeout=180s")
        return True

    def is_active(self, fault: Fault, flags: dict[str, Any] | None = None) -> bool:
        m = fault.mechanism
        if isinstance(m, FlagFault):
            flags = flags if flags is not None else self.flags()
            return bool(flags.get(m.flag, {}).get("defaultVariant", "off") != "off")
        if isinstance(m, ChaosFault):
            out = self.kubectl("get", m.kind.lower(), fault.chaos_name(), "--ignore-not-found", "-o", "name")
            return bool(out.strip())
        out = self.kubectl("get", "deployment", m.deployment, "-o", "json")
        annotations = json.loads(out)["metadata"].get("annotations", {})
        return bool(annotations.get(CHANGE_CAUSE) == m.change_cause)

    def active(self, catalogue: Sequence[Fault] = CATALOGUE) -> list[Fault]:
        flags = self.flags()
        return [f for f in catalogue if self.is_active(f, flags)]

    # --- Prometheus -------------------------------------------------------------------------
    def alerts(self) -> list[dict[str, Any]]:
        """Prometheus alerts that are firing or pending (condition true, waiting out ``for``)."""
        resp = self.http.get(f"{self.prometheus_url}/api/v1/alerts")
        resp.raise_for_status()
        alerts: list[dict[str, Any]] = resp.json()["data"]["alerts"]
        return alerts

    def firing_alerts(self) -> list[dict[str, Any]]:
        return [a for a in self.alerts() if a["state"] == "firing"]

    def query(self, promql: str) -> list[dict[str, Any]]:
        resp = self.http.get(f"{self.prometheus_url}/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        result: list[dict[str, Any]] = resp.json()["data"]["result"]
        return result
