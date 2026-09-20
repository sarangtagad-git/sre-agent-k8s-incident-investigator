"""get_node_status — the agent's equivalent of `kubectl get nodes` / `describe node`.

Cluster-scoped (no namespace — nodes aren't namespaced), and lists every node in
one call since a small lab cluster is cheap to survey in full. Fills the last blind
spot: nothing else in the tool surface can tell the agent a node itself is
NotReady or cordoned, as opposed to an individual pod being unhealthy.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..k8s import load_readonly_clients
from ..observability import get_tracer, truncate_for_trace as _truncate
from .schemas import NodeStatus

_tracer = get_tracer()


_MAX_EVENTS_PER_NODE = 5


def _event_time(e) -> datetime | None:
    """When an event last happened — core/v1 events carry it in a few places depending on
    who emitted them, so take the first that's set."""
    return e.last_timestamp or e.event_time or e.first_timestamp or e.metadata.creation_timestamp


def _age(then: datetime, now: datetime) -> str:
    secs = max(int((now - then).total_seconds()), 0)
    if secs < 90:
        return f"{secs}s ago"
    if secs < 90 * 60:
        return f"{secs // 60}m ago"
    if secs < 48 * 3600:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _events_by_node(raw_events, now: datetime) -> dict[str, list[str]]:
    """Group node events by node name, newest first, capped — each line carries its own
    age so the agent can line a cordon up against pod restart times without guessing."""
    dated = []
    for e in raw_events:
        when = _event_time(e)
        name = e.involved_object.name if e.involved_object else None
        if when is not None and name:
            dated.append((when, name, e))
    dated.sort(key=lambda t: t[0], reverse=True)
    out: dict[str, list[str]] = {}
    for when, name, e in dated:
        lines = out.setdefault(name, [])
        if len(lines) < _MAX_EVENTS_PER_NODE:
            lines.append(
                f"{e.reason} {_age(when, now)} ({when.strftime('%Y-%m-%dT%H:%M:%SZ')}): "
                f"{(e.message or '').strip()}"
            )
    return out


def _taint(t, now: datetime) -> str:
    """"key:effect", plus when it was added if the API recorded it. A cordon's
    `node.kubernetes.io/unschedulable` taint carries `timeAdded` — the durable record of
    WHEN the node was cordoned, which outlives the (hour-long) NodeNotSchedulable event."""
    text = f"{t.key}:{t.effect}"
    added = getattr(t, "time_added", None)
    if added is not None:
        text += f" (added {_age(added, now)}, {added.strftime('%Y-%m-%dT%H:%M:%SZ')})"
    return text


def _node_status(
    n, events: dict[str, list[str]] | None = None, now: datetime | None = None
) -> NodeStatus:
    now = now or datetime.now(timezone.utc)
    conditions = [f"{c.type}={c.status}" for c in (n.status.conditions or [])]
    ready = any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))
    taints = [_taint(t, now) for t in (n.spec.taints or [])]
    return NodeStatus(
        name=n.metadata.name,
        ready=ready,
        schedulable=not bool(n.spec.unschedulable),
        conditions=conditions,
        kubelet_version=(n.status.node_info.kubelet_version if n.status.node_info else None),
        taints=taints,
        recent_events=(events or {}).get(n.metadata.name, []),
    )


def get_node_status(
    clients: dict | None = None, now: datetime | None = None
) -> list[NodeStatus]:
    """Return every node's Ready/schedulable state, conditions, timed taints, and recent events.

    Read-only: uses list (get/list/watch). `schedulable=False` means the node is
    cordoned (`kubectl cordon`) — pods stay where they are but nothing new lands
    there; `ready=False` means the kubelet itself is reporting unhealthy.

    `recent_events` is what says WHEN: the node object only records that a node is
    cordoned, not since when. Node events live in the `default` namespace, so they're
    listed across all namespaces (the agent's read-only role already grants that).
    `now` is injectable so tests get stable ages.
    """
    clients = clients or load_readonly_clients()
    now = now or datetime.now(timezone.utc)
    with _tracer.start_as_current_span("tool.get_node_status") as span:
        span.set_attribute("gen_ai.tool.call.arguments", "{}")
        core = clients["core"]
        raw = core.list_node().items
        events = _events_by_node(
            core.list_event_for_all_namespaces(field_selector="involvedObject.kind=Node").items,
            now,
        )
        nodes = [_node_status(n, events, now) for n in raw]
        span.set_attribute("result.node_count", len(nodes))
        span.set_attribute("result.not_ready_count", sum(1 for n in nodes if not n.ready))
        span.set_attribute("result.cordoned_count", sum(1 for n in nodes if not n.schedulable))
        span.set_attribute(
            "gen_ai.tool.call.result",
            _truncate(json.dumps([n.model_dump(mode="json") for n in nodes])),
        )
        return nodes
