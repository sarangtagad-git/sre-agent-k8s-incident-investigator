"""get_pod_logs — the agent's equivalent of `kubectl logs [--previous]`.

For CrashLoopBackOff, `previous=True` returns the crashed instance's logs (where
the real error lives). Failures (e.g. no previous container) are captured in the
`note` field rather than raised, so they never break an investigation.
"""

from __future__ import annotations

import json

from kubernetes.client.rest import ApiException

from ..k8s import load_readonly_clients
from ..observability import get_tracer
from .schemas import PodLogs

_tracer = get_tracer()


def _api_message(exc: ApiException) -> str:
    try:
        return json.loads(exc.body).get("message", exc.reason or "")
    except Exception:  # noqa: BLE001
        return exc.reason or str(exc)


def get_pod_logs(
    namespace: str,
    name: str,
    container: str | None = None,
    previous: bool = False,
    tail_lines: int = 200,
    since_seconds: int | None = None,
    clients: dict | None = None,
) -> PodLogs:
    """Return recent logs for a pod's container (read-only)."""
    clients = clients or load_readonly_clients()
    core = clients["core"]
    with _tracer.start_as_current_span("tool.get_pod_logs") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.pod", name)
        span.set_attribute("k8s.previous", previous)

        # Resolve the container if not specified (required when a pod has several).
        if container is None:
            pod = core.read_namespaced_pod(name, namespace)
            names = [c.name for c in pod.spec.containers]
            container = names[0] if names else ""

        note: str | None = None
        text = ""
        try:
            text = core.read_namespaced_pod_log(
                name=name,
                namespace=namespace,
                container=container,
                previous=previous,
                tail_lines=tail_lines,
                since_seconds=since_seconds,
                timestamps=False,
            )
        except ApiException as exc:
            note = f"{exc.status} {exc.reason}: {_api_message(exc)}"

        lines = text.splitlines() if text else []
        span.set_attribute("result.line_count", len(lines))
        return PodLogs(
            pod=name,
            namespace=namespace,
            container=container or "",
            previous=previous,
            line_count=len(lines),
            lines=lines,
            note=note,
        )
