"""get_rollout_history — the agent's equivalent of `kubectl rollout history`.

Kubernetes has no single "history" API: kubectl derives it from the ReplicaSets a
Deployment owns, each carrying a `deployment.kubernetes.io/revision` annotation.
We reconstruct that and, importantly, expose the image(s) per revision so the
agent can answer "what changed, and when?".
"""

from __future__ import annotations

from ..k8s import load_readonly_clients
from ..observability import get_tracer
from .schemas import RolloutHistory, RolloutRevision

_tracer = get_tracer()

_REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
_CHANGE_CAUSE_ANNOTATION = "kubernetes.io/change-cause"


def _revision(annotations: dict | None) -> int | None:
    value = (annotations or {}).get(_REVISION_ANNOTATION)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def get_rollout_history(
    namespace: str,
    name: str,
    clients: dict | None = None,
) -> RolloutHistory:
    """Return the revision history (with per-revision images) for a Deployment."""
    clients = clients or load_readonly_clients()
    apps = clients["apps"]
    with _tracer.start_as_current_span("tool.get_rollout_history") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("k8s.deployment", name)

        dep = apps.read_namespaced_deployment(name, namespace)
        dep_uid = dep.metadata.uid
        current_revision = _revision(dep.metadata.annotations)

        replicasets = apps.list_namespaced_replica_set(namespace).items
        revisions: list[RolloutRevision] = []
        for rs in replicasets:
            owners = rs.metadata.owner_references or []
            if not any(o.uid == dep_uid for o in owners):
                continue
            rev = _revision(rs.metadata.annotations)
            if rev is None:
                continue
            images = [c.image for c in rs.spec.template.spec.containers]
            revisions.append(
                RolloutRevision(
                    revision=rev,
                    replicaset=rs.metadata.name,
                    images=images,
                    created=rs.metadata.creation_timestamp,
                    change_cause=(rs.metadata.annotations or {}).get(_CHANGE_CAUSE_ANNOTATION),
                    current=(rev == current_revision),
                    replicas=rs.status.replicas or 0,
                )
            )

        revisions.sort(key=lambda r: r.revision)
        span.set_attribute("result.revision_count", len(revisions))
        return RolloutHistory(
            deployment=name,
            namespace=namespace,
            current_revision=current_revision,
            revisions=revisions,
        )
