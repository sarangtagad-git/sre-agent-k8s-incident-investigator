"""get_pod_events — the agent's equivalent of the Events section of `kubectl describe`."""

from __future__ import annotations

from datetime import datetime, timezone

from ..k8s import load_readonly_clients
from ..observability import get_tracer
from .schemas import PodEvent

_tracer = get_tracer()

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _event(e) -> PodEvent:
    component = None
    if e.source and e.source.component:
        component = e.source.component
    elif getattr(e, "reporting_component", None):
        component = e.reporting_component
    return PodEvent(
        type=e.type or "Normal",
        reason=e.reason,
        message=e.message,
        count=e.count or 1,
        first_seen=e.first_timestamp or e.event_time,
        last_seen=e.last_timestamp or e.event_time,
        component=component,
    )


def get_pod_events(
    namespace: str,
    name: str,
    clients: dict | None = None,
) -> list[PodEvent]:
    """Return events for a single object (usually a pod), newest last.

    Read-only. Mirrors the Events section of `kubectl describe pod`. Note: events
    are ephemeral (~1h TTL), so gather them promptly when an incident fires.
    """
    clients = clients or load_readonly_clients()
    with _tracer.start_as_current_span("tool.get_pod_events") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.object", name)
        raw = clients["core"].list_namespaced_event(
            namespace, field_selector=f"involvedObject.name={name}"
        ).items
        events = [_event(e) for e in raw]
        events.sort(key=lambda ev: ev.last_seen or _EPOCH)
        span.set_attribute("result.event_count", len(events))
        return events
