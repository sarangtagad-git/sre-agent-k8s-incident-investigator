"""Expose the Phase-2 read-only tools to Claude, and dispatch its tool calls.

Three parts:
  ANTHROPIC_TOOLS  — JSON-schema tool definitions Claude sees (its "hands").
  OPENAI_TOOLS     — the same tools, translated to OpenAI function-calling shape,
                      for the optional gather-phase model swap (see graph.py).
                      Derived from ANTHROPIC_TOOLS, not hand-duplicated, so the
                      two can't drift out of sync.
  execute_tool()   — runs the matching Python tool and returns JSON + a record.
                      Provider-agnostic: dispatches on tool name + a plain dict
                      of args, so it's shared by both the Claude and open-model
                      gather paths unchanged.

All tools are read-only (view RBAC). Nothing here can mutate the cluster.
"""

from __future__ import annotations

import json
from typing import Any

from ..observability import get_tracer, truncate_for_trace as _truncate
from ..tools import (
    get_network_policies,
    get_node_status,
    get_pod_events,
    get_pod_logs,
    get_rollout_history,
    get_service_status,
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
            "treat Ready as proof it works.\n"
            "Each container also reports its cpu/memory request and limit straight from "
            "the pod spec. When a service is slow or crashing but its logs/events look "
            "clean, check ITS OWN limits before blaming a dependency — an aggressively "
            "low cpu_limit (e.g. \"5m\") or memory_limit is often the entire root cause "
            "on its own, no metrics backend needed."
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
            "cascades where no pod looks unhealthy.\n"
            "IMPORTANT: when a specific service is slow but its pods look Ready with no "
            "restarts, don't guess from logs/rollout history alone — query THAT service's "
            "own resource metrics before blaming a downstream dependency. CPU throttling "
            "leaves no trace in events or logs, only in metrics: "
            "container_cpu_cfs_throttled_periods_total (is it being throttled?) and "
            "container_spec_cpu_quota / container_spec_cpu_period (what's its limit?), "
            "filtered to the AFFECTED service's own pod — not a service it happens to call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "a PromQL expression"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_network_policies",
        "description": (
            "List NetworkPolicies in a namespace, with pod_selector, every ingress/"
            "egress rule rendered as readable peers + ports, and when each policy "
            "was created.\n"
            "Use when two pods are both Ready and everything else looks healthy, but "
            "traffic between them still fails or times out — a policy may be silently "
            "dropping it. get_workload_status/get_pod_events cannot see this.\n"
            "IMPORTANT: a blocking policy is not automatically the root cause. Check "
            "`created` against the calling service's rollout history (get_rollout_history) "
            "— if the policy is old and stable, the CALLER's traffic pattern is what "
            "changed (fix the app, don't touch the policy); only recommend removing/"
            "editing the policy if it was created right around when the incident started."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"namespace": {"type": "string"}},
            "required": ["namespace"],
        },
    },
    {
        "name": "get_service_status",
        "description": (
            "Describe a Service: its selector, ports, and — critically — "
            "endpoint_count (how many pods actually back it).\n"
            "A Service whose selector matches zero pods looks completely healthy "
            "(ClusterIP assigned, no errors) while routing nowhere. If a Service's "
            "endpoint_count is 0 while matching pods look Ready elsewhere, the "
            "selector doesn't match the pods' labels — check both."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string", "description": "the Service name"},
            },
            "required": ["namespace", "name"],
        },
    },
    {
        "name": "get_node_status",
        "description": (
            "List every node's Ready/schedulable state and conditions (like "
            "`kubectl get nodes`). Cluster-scoped, no namespace argument.\n"
            "Use when pods across multiple namespaces or workloads are unhealthy at "
            "once, or a pod is stuck Pending/evicted — the cause may be the node "
            "itself (NotReady, cordoned, disk/memory pressure), not the workload."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _to_openai_tool(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }


OPENAI_TOOLS: list[dict[str, Any]] = [_to_openai_tool(t) for t in ANTHROPIC_TOOLS]


def _json(obj) -> str:
    if isinstance(obj, list):
        return json.dumps([o.model_dump(mode="json") for o in obj])
    return obj.model_dump_json()




def execute_tool(name: str, tool_input: dict, clients: dict | None) -> tuple[str, bool, ToolRecord]:
    """Run a tool by name. Returns (json_content, is_error, record). Never raises."""
    with _tracer.start_as_current_span(f"agent.tool.{name}") as span:
        span.set_attribute("tool.name", name)
        # gen_ai.tool.call.arguments/result (Opik's mapping for a tool-type span's own
        # Input/Output panels — see GenAIMappingRules.java) — distinct from the plain
        # tool.name/tool.ok attributes below, which only drive metadata, not content.
        span.set_attribute("gen_ai.tool.call.arguments", _truncate(json.dumps(tool_input)))
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
            elif name == "get_network_policies":
                res = get_network_policies(tool_input["namespace"], clients=clients)
                summary = f"{len(res)} network policies"
            elif name == "get_service_status":
                res = get_service_status(tool_input["namespace"], tool_input["name"], clients=clients)
                summary = f"{res.endpoint_count} endpoints ({res.ready_endpoint_count} ready)"
            elif name == "get_node_status":
                res = get_node_status(clients=clients)
                summary = f"{len(res)} nodes, {sum(1 for n in res if not n.ready)} not ready"
            else:
                return (json.dumps({"error": f"unknown tool {name}"}), True, ToolRecord(tool=name, ok=False, summary="unknown tool"))

            content = _json(res)
            record = ToolRecord(tool=name, input=tool_input, ok=True, summary=summary)
            span.set_attribute("tool.ok", True)
            span.set_attribute("gen_ai.tool.call.result", _truncate(content))
            return content, False, record
        except Exception as exc:  # noqa: BLE001 — return the error to the model, don't crash the run
            span.set_attribute("tool.ok", False)
            error_content = json.dumps({"error": str(exc)})
            span.set_attribute("gen_ai.tool.call.result", error_content)
            return (
                error_content,
                True,
                ToolRecord(tool=name, input=tool_input, ok=False, summary=f"error: {exc}"),
            )
