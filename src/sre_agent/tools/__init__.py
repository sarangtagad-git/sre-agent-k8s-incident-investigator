"""Read-only evidence-gathering tools — the agent's "hands".

Each tool wraps a manual investigation step and returns STRUCTURED facts
(Pydantic models) for the LLM to reason over. K8s tools use the read-only client
from sre_agent.k8s (view RBAC only); query_prometheus uses the Prometheus HTTP API.

All five Phase-2 tools:
- get_workload_status  <- kubectl get pods / get deploy
- get_pod_events       <- kubectl describe / get events
- get_pod_logs         <- kubectl logs (--previous)
- get_rollout_history  <- kubectl rollout history
- query_prometheus     <- Prometheus HTTP API (PromQL)
"""

from .events import get_pod_events
from .logs import get_pod_logs
from .metrics import query_prometheus, query_prometheus_range
from .rollout import get_rollout_history
from .schemas import (
    ContainerState,
    DeploymentStatus,
    MetricSample,
    MetricSeries,
    NamespaceWorkloadStatus,
    PodEvent,
    PodLogs,
    PodStatus,
    PrometheusRangeResult,
    PrometheusResult,
    RolloutHistory,
    RolloutRevision,
)
from .workload import get_workload_status

__all__ = [
    # tools
    "get_workload_status",
    "get_pod_events",
    "get_pod_logs",
    "get_rollout_history",
    "query_prometheus",
    "query_prometheus_range",
    # schemas
    "PodStatus",
    "ContainerState",
    "DeploymentStatus",
    "NamespaceWorkloadStatus",
    "PodEvent",
    "PodLogs",
    "RolloutRevision",
    "RolloutHistory",
    "MetricSample",
    "PrometheusResult",
    "MetricSeries",
    "PrometheusRangeResult",
]
