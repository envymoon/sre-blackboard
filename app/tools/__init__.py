"""Read-only tools for the investigators (M3; kb_search since M6): registry, evidence store, record/replay."""

from __future__ import annotations

from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolOutcome
from app.tools.flags import FLAG_STATE
from app.tools.jaeger import TRACE_GET, TRACES_SEARCH
from app.tools.k8s import DEPLOY_DIFF, K8S_EVENTS, ROLLOUT_HISTORY
from app.tools.kb import KB_SEARCH
from app.tools.loki import LOGS_SEARCH
from app.tools.prometheus import PROM_QUERY, PROM_RANGE
from app.tools.registry import ToolRegistry

ALL_TOOLS: tuple[Tool, ...] = (
    PROM_QUERY,
    PROM_RANGE,
    LOGS_SEARCH,
    TRACES_SEARCH,
    TRACE_GET,
    K8S_EVENTS,
    ROLLOUT_HISTORY,
    DEPLOY_DIFF,
    FLAG_STATE,
    KB_SEARCH,
)

__all__ = ["ALL_TOOLS", "Tool", "ToolArgs", "ToolContext", "ToolError", "ToolOutcome", "ToolRegistry"]
