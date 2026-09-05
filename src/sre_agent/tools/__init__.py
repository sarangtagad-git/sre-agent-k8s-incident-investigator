"""Read-only evidence-gathering tools — the agent's "hands".

Each tool wraps a manual investigation step and returns STRUCTURED facts
(Pydantic models) for the LLM to reason over. K8s tools use the read-only client
from sre_agent.k8s (view RBAC only); query_prometheus uses the Prometheus HTTP API.

All eight tools:
- get_workload_status   <- kubectl get pods / get deploy
- get_pod_events        <- kubectl describe / get events
- get_pod_logs          <- kubectl logs (--previous)
- get_rollout_history   <- kubectl rollout history
- query_prometheus      <- Prometheus HTTP API (PromQL)
- get_network_policies  <- kubectl describe networkpolicy (Batch-1 incident taxonomy)
- get_service_status    <- kubectl describe service (Batch-1 incident taxonomy)
- get_node_status       <- kubectl get nodes / describe node (Batch-1 incident taxonomy)
"""

from .events import get_pod_events
from .logs import get_pod_logs
from .metrics import query_prometheus, query_prometheus_range
from .network import get_network_policies
from .node import get_node_status
from .rollout import get_rollout_history
from .schemas import (
    ContainerState,
    DeploymentStatus,
    MetricSample,
    MetricSeries,
    NamespaceWorkloadStatus,
    NetworkPolicyInfo,
    NetworkPolicyRule,
    NodeStatus,
    PodEvent,
    PodLogs,
    PodStatus,
    PrometheusRangeResult,
    PrometheusResult,
    RolloutHistory,
    RolloutRevision,
    ServiceStatus,
)
from .service import get_service_status
from .workload import get_workload_status

__all__ = [
    # tools
    "get_workload_status",
    "get_pod_events",
    "get_pod_logs",
    "get_rollout_history",
    "query_prometheus",
    "query_prometheus_range",
    "get_network_policies",
    "get_service_status",
    "get_node_status",
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
    "NetworkPolicyRule",
    "NetworkPolicyInfo",
    "ServiceStatus",
    "NodeStatus",
]
