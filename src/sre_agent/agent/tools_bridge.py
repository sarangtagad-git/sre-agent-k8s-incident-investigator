"""Expose the Phase-2 read-only tools to Claude, and dispatch its tool calls.

Two halves:
  ANTHROPIC_TOOLS  — JSON-schema tool definitions Claude sees (its "hands").
  execute_tool()   — runs the matching Python tool and returns JSON + a record.

All tools are read-only (view RBAC). Nothing here can mutate the cluster.
"""

from __future__ import annotations

import json
from typing import Any

from ..observability import get_tracer
from ..tools import (
    get_pod_events,
    get_pod_logs,
    get_rollout_history,
    get_workload_status,
    query_prometheus,
)
from .schemas import ToolRecord

_tracer = get_tracer()

ANTHROPIC_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_workload_status",
        "description": (
            "List deployments and pods in a namespace (like `kubectl get deploy` + "
            "`get pods`).\n"
            "Returns status, ready, restarts, and a headline `reason` per pod "
            "(e.g. ImagePullBackOff, CrashLoopBackOff). Start here.\n"
            "NOTE: a pod can be Ready and still be failing its dependency — do not "
            "treat Ready as proof it works."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "selector": {"type": "string", "description": "optional label selector, e.g. app=frontend"},
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "get_pod_events",
        "description": (
            "Get Kubernetes events for a pod (the Events section of `kubectl describe`).\n"
            "The smoking gun for ImagePullBackOff (reason/message names why the pull "
            "failed: NotFound vs auth vs network) and scheduling failures.\n"
            "Events are ephemeral (~1h)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string", "description": "the pod (or object) name"},
            },
            "required": ["namespace", "name"],
        },
    },
    {
        "name": "get_pod_logs",
        "description": (
            "Get a pod container's logs (`kubectl logs`).\n"
            "For CrashLoopBackOff set previous=true to read the crashed instance's "
            "logs (where the real error is).\n"
            "For ImagePullBackOff logs are useless (nothing ran) — use events instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
                "container": {"type": "string", "description": "optional; defaults to the first container"},
                "previous": {"type": "boolean", "description": "logs of the previous/crashed instance"},
                "tail_lines": {"type": "integer", "description": "how many lines (default 100)"},
            },
            "required": ["namespace", "name"],
        },
    },
    {
        "name": "get_rollout_history",
        "description": (
            "Deployment revision history with the image(s) per revision "
            "(`kubectl rollout history`).\n"
            "Answers 'what changed and when?' — e.g. an image tag flipped to a "
            "broken value at revision N."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "deployment": {"type": "string"},
            },
            "required": ["namespace", "deployment"],
        },
    },
    {
        "name": "query_prometheus",
        "description": (
            "Run an instant PromQL query against Prometheus (golden signals: error "
            "rate, latency, saturation).\n"
            "Use to quantify user impact and correlate timing, especially for "
            "cascades where no pod looks unhealthy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "a PromQL expression"}},
            "required": ["query"],
        },
    },
]


def _json(obj) -> str:
    if isinstance(obj, list):
        return json.dumps([o.model_dump(mode="json") for o in obj])
    return obj.model_dump_json()


def execute_tool(name: str, tool_input: dict, clients: dict | None) -> tuple[str, bool, ToolRecord]:
    """Run a tool by name. Returns (json_content, is_error, record). Never raises."""
    with _tracer.start_as_current_span(f"agent.tool.{name}") as span:
        span.set_attribute("tool.name", name)
        try:
            if name == "get_workload_status":
                res = get_workload_status(
                    tool_input["namespace"], tool_input.get("selector"), clients=clients
                )
                summary = f"{len(res.pods)} pods, {len(res.deployments)} deployments"
            elif name == "get_pod_events":
                res = get_pod_events(tool_input["namespace"], tool_input["name"], clients=clients)
                summary = f"{len(res)} events"
            elif name == "get_pod_logs":
                res = get_pod_logs(
                    tool_input["namespace"],
                    tool_input["name"],
                    container=tool_input.get("container"),
                    previous=bool(tool_input.get("previous", False)),
                    tail_lines=int(tool_input.get("tail_lines", 100)),
                    clients=clients,
                )
                summary = f"{res.line_count} log lines" + (f" ({res.note})" if res.note else "")
            elif name == "get_rollout_history":
                res = get_rollout_history(
                    tool_input["namespace"], tool_input["deployment"], clients=clients
                )
                summary = f"{len(res.revisions)} revisions"
            elif name == "query_prometheus":
                res = query_prometheus(tool_input["query"])
                summary = res.error or f"{len(res.samples)} samples"
            else:
                return (json.dumps({"error": f"unknown tool {name}"}), True, ToolRecord(tool=name, ok=False, summary="unknown tool"))

            content = _json(res)
            record = ToolRecord(tool=name, input=tool_input, ok=True, summary=summary)
            span.set_attribute("tool.ok", True)
            return content, False, record
        except Exception as exc:  # noqa: BLE001 — return the error to the model, don't crash the run
            span.set_attribute("tool.ok", False)
            return (
                json.dumps({"error": str(exc)}),
                True,
                ToolRecord(tool=name, input=tool_input, ok=False, summary=f"error: {exc}"),
            )
