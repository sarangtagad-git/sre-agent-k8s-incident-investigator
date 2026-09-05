"""get_service_status — the agent's equivalent of `kubectl describe service`.

The key signal is endpoint_count: a Service whose selector matches zero pods still
looks completely healthy on its own (ClusterIP assigned, no error anywhere) while
silently routing traffic nowhere. Nothing else in the tool surface would catch a
Service/pod label mismatch — get_workload_status looks at pods, not at whether any
Service actually points at them.
"""

from __future__ import annotations

import json

from ..k8s import load_readonly_clients
from ..observability import get_tracer, truncate_for_trace as _truncate
from .schemas import ServiceStatus

_tracer = get_tracer()


def _port_str(port) -> str:
    target = port.target_port
    proto = port.protocol or "TCP"
    return f"{port.port}->{target}/{proto}"


def get_service_status(
    namespace: str,
    name: str,
    clients: dict | None = None,
) -> ServiceStatus:
    """Return a Service's spec plus how many pods are actually backing it.

    Read-only: uses get (get/list/watch). endpoint_count/ready_endpoint_count come
    from the classic Endpoints object (not EndpointSlices) — simpler, and RBAC
    already grants it directly.
    """
    clients = clients or load_readonly_clients()
    core = clients["core"]
    with _tracer.start_as_current_span("tool.get_service_status") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.service", name)
        span.set_attribute("gen_ai.tool.call.arguments", json.dumps({"namespace": namespace, "name": name}))

        svc = core.read_namespaced_service(name, namespace)

        endpoint_count = ready_count = 0
        try:
            ep = core.read_namespaced_endpoints(name, namespace)
            for subset in ep.subsets or []:
                ready_count += len(subset.addresses or [])
                endpoint_count += len(subset.addresses or []) + len(subset.not_ready_addresses or [])
        except Exception:  # noqa: BLE001 — no Endpoints object yet is a valid state, not an error
            pass

        result = ServiceStatus(
            name=name,
            namespace=namespace,
            type=svc.spec.type or "ClusterIP",
            selector=dict(svc.spec.selector or {}),
            cluster_ip=svc.spec.cluster_ip,
            ports=[_port_str(p) for p in (svc.spec.ports or [])],
            endpoint_count=endpoint_count,
            ready_endpoint_count=ready_count,
        )
        span.set_attribute("result.endpoint_count", endpoint_count)
        span.set_attribute("gen_ai.tool.call.result", _truncate(result.model_dump_json()))
        return result
