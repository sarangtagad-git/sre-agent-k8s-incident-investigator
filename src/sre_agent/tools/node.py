"""get_node_status — the agent's equivalent of `kubectl get nodes` / `describe node`.

Cluster-scoped (no namespace — nodes aren't namespaced), and lists every node in
one call since a small lab cluster is cheap to survey in full. Fills the last blind
spot: nothing else in the tool surface can tell the agent a node itself is
NotReady or cordoned, as opposed to an individual pod being unhealthy.
"""

from __future__ import annotations

import json

from ..k8s import load_readonly_clients
from ..observability import get_tracer, truncate_for_trace as _truncate
from .schemas import NodeStatus

_tracer = get_tracer()


def _node_status(n) -> NodeStatus:
    conditions = [f"{c.type}={c.status}" for c in (n.status.conditions or [])]
    ready = any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))
    return NodeStatus(
        name=n.metadata.name,
        ready=ready,
        schedulable=not bool(n.spec.unschedulable),
        conditions=conditions,
        kubelet_version=(n.status.node_info.kubelet_version if n.status.node_info else None),
    )


def get_node_status(clients: dict | None = None) -> list[NodeStatus]:
    """Return every node's Ready/schedulable state and conditions.

    Read-only: uses list (get/list/watch). `schedulable=False` means the node is
    cordoned (`kubectl cordon`) — pods stay where they are but nothing new lands
    there; `ready=False` means the kubelet itself is reporting unhealthy.
    """
    clients = clients or load_readonly_clients()
    with _tracer.start_as_current_span("tool.get_node_status") as span:
        span.set_attribute("gen_ai.tool.call.arguments", "{}")
        raw = clients["core"].list_node().items
        nodes = [_node_status(n) for n in raw]
        span.set_attribute("result.node_count", len(nodes))
        span.set_attribute("result.not_ready_count", sum(1 for n in nodes if not n.ready))
        span.set_attribute(
            "gen_ai.tool.call.result",
            _truncate(json.dumps([n.model_dump(mode="json") for n in nodes])),
        )
        return nodes
