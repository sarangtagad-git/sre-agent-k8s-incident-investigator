"""get_workload_status — the agent's equivalent of `kubectl get pods / get deploy`."""

from __future__ import annotations

from ..k8s import load_readonly_clients
from ..observability import get_tracer
from .schemas import ContainerState, DeploymentStatus, NamespaceWorkloadStatus, PodStatus

_tracer = get_tracer()


def _container_state(cs) -> ContainerState:
    state, reason, message, exit_code = "unknown", None, None, None
    if cs.state:
        if cs.state.waiting:
            state, reason, message = "waiting", cs.state.waiting.reason, cs.state.waiting.message
        elif cs.state.terminated:
            state = "terminated"
            reason = cs.state.terminated.reason
            exit_code = cs.state.terminated.exit_code
        elif cs.state.running:
            state = "running"
    last_reason, last_exit = None, None
    if cs.last_state and cs.last_state.terminated:
        last_reason = cs.last_state.terminated.reason
        last_exit = cs.last_state.terminated.exit_code
    return ContainerState(
        name=cs.name,
        ready=bool(cs.ready),
        restart_count=cs.restart_count or 0,
        state=state,
        reason=reason,
        message=message,
        exit_code=exit_code,
        last_reason=last_reason,
        last_exit_code=last_exit,
    )


def _pod_status(pod) -> PodStatus:
    st = pod.status
    ready = False
    for cond in st.conditions or []:
        if cond.type == "Ready":
            ready = cond.status == "True"
    containers = [_container_state(cs) for cs in (st.container_statuses or [])]
    restarts = sum(c.restart_count for c in containers)
    # Headline reason: the first not-ready container's reason (waiting or terminated).
    reason = next((c.reason for c in containers if not c.ready and c.reason), None)
    return PodStatus(
        name=pod.metadata.name,
        namespace=pod.metadata.namespace,
        phase=st.phase or "Unknown",
        ready=ready,
        restarts=restarts,
        node=pod.spec.node_name if pod.spec else None,
        start_time=st.start_time,
        reason=reason,
        containers=containers,
    )


def _deployment_status(d) -> DeploymentStatus:
    s = d.status
    progressing = available = None
    for c in s.conditions or []:
        if c.type == "Progressing":
            progressing = c.status == "True"
        elif c.type == "Available":
            available = c.status == "True"
    return DeploymentStatus(
        name=d.metadata.name,
        namespace=d.metadata.namespace,
        desired=d.spec.replicas or 0,
        ready=s.ready_replicas or 0,
        available=s.available_replicas or 0,
        updated=s.updated_replicas or 0,
        unavailable=s.unavailable_replicas or 0,
        progressing=progressing,
        available_condition=available,
    )


def get_workload_status(
    namespace: str,
    selector: str | None = None,
    clients: dict | None = None,
) -> NamespaceWorkloadStatus:
    """Return deployments + pods for a namespace (optionally filtered by label selector).

    Read-only: uses list (get/list/watch). Mirrors `kubectl get deploy` + `get pods`.
    """
    clients = clients or load_readonly_clients()
    with _tracer.start_as_current_span("tool.get_workload_status") as span:
        span.set_attribute("k8s.namespace", namespace)
        if selector:
            span.set_attribute("k8s.selector", selector)
        pods = clients["core"].list_namespaced_pod(namespace, label_selector=selector).items
        deploys = clients["apps"].list_namespaced_deployment(namespace, label_selector=selector).items
        result = NamespaceWorkloadStatus(
            namespace=namespace,
            deployments=[_deployment_status(d) for d in deploys],
            pods=[_pod_status(p) for p in pods],
        )
        span.set_attribute("result.pod_count", len(result.pods))
        span.set_attribute("result.deployment_count", len(result.deployments))
        return result
