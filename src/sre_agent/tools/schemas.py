"""Typed return shapes for the read-only tools (the agent's "ground truth").

Every tool returns one of these Pydantic models instead of raw text, so the LLM
reasons over clean fields (e.g. reason="ImagePullBackOff") and we get validation,
type-safety, and tidy JSON for free.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ContainerState(BaseModel):
    """State of a single container inside a pod."""

    name: str
    ready: bool
    restart_count: int
    state: str  # "running" | "waiting" | "terminated"
    reason: str | None = None  # e.g. ImagePullBackOff, CrashLoopBackOff, Error
    message: str | None = None
    exit_code: int | None = None
    # From the *previous* (crashed) instance — key for CrashLoopBackOff.
    last_reason: str | None = None
    last_exit_code: int | None = None
    # What was actually asked for, straight from the pod spec — not a live usage number.
    # A resource-starvation root cause (throttled CPU, silent OOM risk) is often visible
    # here alone: a limit of "5m" CPU or "16Mi" memory is a smoking gun on its own,
    # before ever needing a metrics backend.
    cpu_limit: str | None = None
    cpu_request: str | None = None
    memory_limit: str | None = None
    memory_request: str | None = None


class PodStatus(BaseModel):
    """A pod's health at a glance — mirrors what you read from `kubectl get pods`."""

    name: str
    namespace: str
    phase: str  # Pending, Running, Succeeded, Failed, Unknown
    ready: bool  # pod-level Ready condition
    restarts: int  # summed across containers
    node: str | None = None
    start_time: datetime | None = None
    reason: str | None = None  # first not-ready container's reason (the headline)
    containers: list[ContainerState] = Field(default_factory=list)


class DeploymentStatus(BaseModel):
    """Rollout health of a Deployment — mirrors `kubectl get deploy`."""

    name: str
    namespace: str
    desired: int
    ready: int
    available: int
    updated: int
    unavailable: int
    progressing: bool | None = None  # from the Progressing condition
    available_condition: bool | None = None  # from the Available condition


class NamespaceWorkloadStatus(BaseModel):
    """What `get_workload_status` returns: deployments + pods for a namespace."""

    namespace: str
    deployments: list[DeploymentStatus] = Field(default_factory=list)
    pods: list[PodStatus] = Field(default_factory=list)


class PodEvent(BaseModel):
    """A single Kubernetes event — the smoking gun for many incidents."""

    type: str  # Normal | Warning
    reason: str | None = None
    message: str | None = None
    count: int = 1
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    component: str | None = None  # who emitted it (kubelet, default-scheduler, ...)


class PodLogs(BaseModel):
    """Container logs — mirrors `kubectl logs [--previous]`."""

    pod: str
    namespace: str
    container: str
    previous: bool = False
    line_count: int = 0
    lines: list[str] = Field(default_factory=list)
    note: str | None = None  # e.g. "no previous terminated container"


class RolloutRevision(BaseModel):
    """One revision of a Deployment (backed by one ReplicaSet)."""

    revision: int
    replicaset: str
    images: list[str] = Field(default_factory=list)
    created: datetime | None = None
    change_cause: str | None = None
    current: bool = False  # is this the Deployment's live revision?
    replicas: int = 0  # current replicas on this ReplicaSet


class RolloutHistory(BaseModel):
    """Deployment rollout history — mirrors `kubectl rollout history`.

    Crucially exposes the image(s) per revision, so "what changed" (e.g. an image
    tag flipped to :v0.0.0-broken at revision N) is a clean, comparable field.
    """

    deployment: str
    namespace: str
    current_revision: int | None = None
    revisions: list[RolloutRevision] = Field(default_factory=list)


class MetricSample(BaseModel):
    """One instant metric value with its label set (a Prometheus vector element)."""

    labels: dict[str, str] = Field(default_factory=dict)
    value: float
    timestamp: float  # unix seconds


class PrometheusResult(BaseModel):
    """Result of an instant PromQL query — mirrors `/api/v1/query`."""

    query: str
    result_type: str = ""  # vector | scalar | matrix | string
    samples: list[MetricSample] = Field(default_factory=list)
    error: str | None = None  # captured, not raised — never breaks an investigation


class MetricSeries(BaseModel):
    """A single time series (labels + points over time)."""

    labels: dict[str, str] = Field(default_factory=dict)
    values: list[tuple[float, float]] = Field(default_factory=list)  # (timestamp, value)


class PrometheusRangeResult(BaseModel):
    """Result of a range PromQL query — mirrors `/api/v1/query_range`."""

    query: str
    series: list[MetricSeries] = Field(default_factory=list)
    error: str | None = None


class NetworkPolicyRule(BaseModel):
    """One ingress or egress rule within a NetworkPolicy, in human-readable form."""

    direction: str  # "ingress" | "egress"
    peers: list[str] = Field(default_factory=list)  # e.g. "pods where app=paymentservice"
    ports: list[str] = Field(default_factory=list)  # e.g. "TCP/50051"


class NetworkPolicyInfo(BaseModel):
    """A NetworkPolicy — mirrors `kubectl describe networkpolicy`.

    The smoking gun for traffic that's silently dropped between two healthy pods:
    check whether a policy's pod_selector matches the destination and whether any
    of its rules actually admit the source.

    `created` matters for root-causing, not just describing: a policy that's been
    stable for weeks is probably intentional (the caller's traffic pattern is what
    changed — fix the app, don't touch the policy); a policy created minutes ago,
    right as the incident started, is probably the change that broke things.
    Compare it against the calling service's own rollout history to tell which.
    """

    name: str
    namespace: str
    pod_selector: str  # e.g. "app=paymentservice", or "<all pods>" if empty
    policy_types: list[str] = Field(default_factory=list)  # ["Ingress"], ["Egress"], or both
    rules: list[NetworkPolicyRule] = Field(default_factory=list)
    created: datetime | None = None


class ServiceStatus(BaseModel):
    """A Service's routing health — mirrors `kubectl describe service`.

    endpoint_count is the key signal: a Service whose selector matches zero pods
    still looks perfectly healthy on its own (ClusterIP assigned, no errors) while
    silently routing nowhere — the classic "wrong_service_selector" failure mode.
    """

    name: str
    namespace: str
    type: str  # ClusterIP | NodePort | LoadBalancer
    selector: dict[str, str] = Field(default_factory=dict)
    cluster_ip: str | None = None
    ports: list[str] = Field(default_factory=list)  # e.g. "80->8080/TCP"
    endpoint_count: int = 0  # ready + not-ready addresses actually backing this Service
    ready_endpoint_count: int = 0


class NodeStatus(BaseModel):
    """A cluster node's health — mirrors `kubectl get nodes` / `describe node`."""

    name: str
    ready: bool
    schedulable: bool  # False if cordoned (spec.unschedulable)
    conditions: list[str] = Field(default_factory=list)  # e.g. "MemoryPressure=False"
    kubelet_version: str | None = None
