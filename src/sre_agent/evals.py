"""Phase 6: the incident eval harness — data + scoring (pure, no cluster or API key).

Each Incident is a reproducible failure: how to `stage` it, how to `revert` it, what to ask
the agent, and the ground truth its RCA must match. The CLI `sre-agent eval` command stages
each one, runs the real agent, scores the report with `score()` here, and always reverts.

Keeping the specs and scoring here (importable without anthropic/langgraph) means the scoring
logic gets fast, deterministic unit tests even though the full eval needs a live cluster + key.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .agent.schemas import IncidentContext, RCAReport
from .remediation import validate_remediation


@dataclass
class Incident:
    """A reproducible incident + the ground truth the agent's RCA must match."""

    name: str
    description: str
    stage: list[list[str]]  # kubectl arg-lists run with the admin context to break the cluster
    revert: list[list[str]]  # arg-lists to restore it (always run, even on failure)
    context: IncidentContext  # what the agent is asked to investigate
    wait_seconds: int
    expect_categories: set[str]  # RCA category must be one of these (critical)
    must_include: list[str] = field(default_factory=list)  # all must appear in the RCA text
    must_include_any: list[str] = field(default_factory=list)  # at least one must appear
    min_score: float = 0.6  # confidence_score floor (informational)


@dataclass
class Check:
    name: str
    passed: bool
    critical: bool
    detail: str = ""


def _haystack(report: RCAReport) -> str:
    parts = [report.summary, report.root_cause, report.impact]
    parts += report.evidence + report.alternatives
    return " ".join(parts).lower()


def score(report: RCAReport, incident: Incident) -> list[Check]:
    """Assert an RCA against an incident's ground truth. Critical checks gate pass/fail."""
    hay = _haystack(report)
    checks: list[Check] = []

    checks.append(
        Check(
            "category",
            report.category in incident.expect_categories,
            critical=True,
            detail=f"got {report.category!r}, expected one of {sorted(incident.expect_categories)}",
        )
    )

    has_all = all(s.lower() in hay for s in incident.must_include)
    has_any = not incident.must_include_any or any(
        s.lower() in hay for s in incident.must_include_any
    )
    checks.append(
        Check(
            "root_cause_match",
            has_all and has_any,
            critical=True,
            detail=f"must_include={incident.must_include} any-of={incident.must_include_any}",
        )
    )

    decision = validate_remediation(report.remediation.command)
    checks.append(
        Check(
            "remediation_gate_valid",
            decision.allowed,
            critical=False,
            detail=decision.reason if not decision.allowed else report.remediation.command,
        )
    )

    checks.append(
        Check(
            "confidence",
            report.confidence_score >= incident.min_score,
            critical=False,
            detail=f"score={report.confidence_score:.2f} (floor {incident.min_score})",
        )
    )
    return checks


def incident_passed(checks: list[Check]) -> bool:
    """An incident passes iff every critical check passed."""
    return all(c.passed for c in checks if c.critical)


_NS = ["-n", "boutique"]

INCIDENTS: list[Incident] = [
    Incident(
        name="image_pull",
        description="Bad image tag → ImagePullBackOff on a new rollout (old pod still serves).",
        stage=[
            ["kubectl", *_NS, "set", "image", "deployment/currencyservice",
             "*=nonexistent.invalid/currencyservice:v0-broken-sre-eval"],
        ],
        revert=[["kubectl", *_NS, "rollout", "undo", "deployment/currencyservice"]],
        context=IncidentContext(
            namespace="boutique",
            workload="currencyservice",
            alert="currencyservice rollout not progressing (new pod not becoming Ready)",
            skip_recall=True,  # eval incidents stay memory-blind — see docs/memory-plan.md
        ),
        wait_seconds=30,
        expect_categories={"rollout", "config"},
        must_include_any=["image", "pull", "tag", "registry", "imagepull", "manifest"],
    ),
    Incident(
        name="crash_loop",
        description="Bad container command → CrashLoopBackOff with a ModuleNotFoundError in logs.",
        stage=[
            ["kubectl", *_NS, "patch", "deployment/emailservice", "--type=json", "-p",
             '[{"op":"add","path":"/spec/template/spec/containers/0/command",'
             '"value":["python","-c","import nonexistent_sre_eval_module"]}]'],
        ],
        revert=[["kubectl", *_NS, "rollout", "undo", "deployment/emailservice"]],
        context=IncidentContext(
            namespace="boutique",
            workload="emailservice",
            alert="emailservice pods CrashLoopBackOff, restart count climbing",
            skip_recall=True,  # eval incidents stay memory-blind — see docs/memory-plan.md
        ),
        wait_seconds=35,
        expect_categories={"workload", "config", "rollout"},
        must_include_any=["crash", "module", "import", "command", "exit", "restart", "previous"],
    ),
    Incident(
        name="cascade",
        description="redis-cart scaled to 0 → cartservice/checkout 500s while pods stay Ready.",
        stage=[["kubectl", *_NS, "scale", "deployment/redis-cart", "--replicas=0"]],
        revert=[["kubectl", *_NS, "scale", "deployment/redis-cart", "--replicas=1"]],
        context=IncidentContext(
            namespace="boutique",
            workload=None,  # no hint: the agent must trace the dependency chain itself
            alert="checkout and cart requests failing with 500s; frontend up",
            skip_recall=True,  # eval incidents stay memory-blind — see docs/memory-plan.md
        ),
        wait_seconds=40,
        expect_categories={"dependency"},
        must_include=["redis"],
        must_include_any=["scale", "0 replica", "zero", "no pod", "unavailable", "backend", "desired=0"],
    ),
    # --- Batch 1 additions (docs/incident-taxonomy-plan.md) ---
    Incident(
        name="oom_killed",
        description="Memory limit set far too low → recommendationservice gets OOMKilled repeatedly.",
        stage=[
            ["kubectl", *_NS, "patch", "deployment/recommendationservice", "--type=json", "-p",
             '[{"op":"add","path":"/spec/template/spec/containers/0/resources",'
             '"value":{"limits":{"memory":"10Mi"},"requests":{"memory":"10Mi"}}}]'],
        ],
        revert=[["kubectl", *_NS, "rollout", "undo", "deployment/recommendationservice"]],
        context=IncidentContext(
            namespace="boutique",
            workload="recommendationservice",
            alert="recommendationservice pods restarting repeatedly, restart count climbing",
            skip_recall=True,
        ),
        wait_seconds=30,
        expect_categories={"saturation", "workload"},
        must_include_any=["oom", "oomkilled", "memory", "out of memory"],
    ),
    Incident(
        name="cpu_throttled",
        description="CPU limit set far too low → checkoutservice works but is throttled/slow.",
        stage=[
            ["kubectl", *_NS, "patch", "deployment/checkoutservice", "--type=json", "-p",
             '[{"op":"add","path":"/spec/template/spec/containers/0/resources",'
             '"value":{"limits":{"cpu":"5m"},"requests":{"cpu":"5m"}}}]'],
        ],
        revert=[["kubectl", *_NS, "rollout", "undo", "deployment/checkoutservice"]],
        context=IncidentContext(
            namespace="boutique",
            workload="checkoutservice",
            alert="checkout requests succeeding but noticeably slow; latency climbing",
            skip_recall=True,
        ),
        wait_seconds=45,
        # "config" included alongside "saturation": live-verified 2026-08-20, a fully
        # correct diagnosis (right service, right 5m limit, right throttling %) still
        # got labeled "config" (misconfigured limit) instead of "saturation" (resource
        # exhaustion) — both are defensible names for the same correct finding.
        expect_categories={"saturation", "config"},
        # must_include=["checkoutservice"] is load-bearing, not decorative: live-verified
        # 2026-08-20, the agent produced a plausible-sounding but WRONG RCA blaming
        # emailservice's missing memory limit for the latency, and that answer still
        # passed on keywords alone ("limit", "latency" both appear, just for the wrong
        # reason). Naming the actually-affected service is the only thing that catches
        # a coherent wrong answer, not just an incoherent one.
        must_include=["checkoutservice"],
        must_include_any=["cpu", "throttl", "limit", "latency"],
    ),
    Incident(
        name="unschedulable",
        description="New pod asks for far more CPU than any node has → stuck Pending.",
        stage=[
            ["kubectl", *_NS, "patch", "deployment/paymentservice", "--type=json", "-p",
             '[{"op":"add","path":"/spec/template/spec/containers/0/resources",'
             '"value":{"requests":{"cpu":"100"}}}]'],
        ],
        revert=[["kubectl", *_NS, "rollout", "undo", "deployment/paymentservice"]],
        context=IncidentContext(
            namespace="boutique",
            workload="paymentservice",
            alert="paymentservice's new pod stuck Pending, never starts",
            skip_recall=True,
        ),
        wait_seconds=20,
        # "saturation" included alongside "scheduling": live-verified 2026-08-20, the
        # agent correctly found "all 3 nodes have exhausted allocatable CPU" but labeled
        # it "saturation" (resource exhaustion) instead of "scheduling" (can't place the
        # pod) — both are defensible names for the same correct finding.
        expect_categories={"scheduling", "saturation"},
        must_include_any=["schedul", "insufficient", "cpu", "pending", "capacity", "node"],
    ),
    Incident(
        name="bad_config",
        description="Pod references a Secret that doesn't exist → CreateContainerConfigError.",
        stage=[
            ["kubectl", *_NS, "patch", "deployment/cartservice", "--type=json", "-p",
             '[{"op":"add","path":"/spec/template/spec/containers/0/envFrom",'
             '"value":[{"secretRef":{"name":"nonexistent-sre-eval-secret"}}]}]'],
        ],
        revert=[["kubectl", *_NS, "rollout", "undo", "deployment/cartservice"]],
        context=IncidentContext(
            namespace="boutique",
            workload="cartservice",
            alert="cartservice pods stuck, never becoming Ready",
            skip_recall=True,
        ),
        wait_seconds=20,
        expect_categories={"config"},
        must_include_any=["secret", "config", "not found", "missing", "createcontainerconfigerror"],
    ),
    Incident(
        name="network_blocked",
        description="A NetworkPolicy silently drops all traffic into paymentservice; pods stay healthy.",
        stage=[["kubectl", *_NS, "apply", "-f", "infra/eval-incidents/network-blocked-policy.yaml"]],
        revert=[["kubectl", *_NS, "delete", "-f", "infra/eval-incidents/network-blocked-policy.yaml"]],
        context=IncidentContext(
            namespace="boutique",
            workload=None,  # no hint: the agent must find which service traffic is being dropped to
            alert="checkout fails at the payment step; frontend, checkout, and payment pods all look healthy",
            skip_recall=True,
        ),
        wait_seconds=30,
        expect_categories={"networking"},
        must_include=["payment"],
        must_include_any=["networkpolicy", "network policy", "blocked", "denied", "ingress rule"],
    ),
    Incident(
        name="wrong_service_selector",
        description="frontend's Service selector no longer matches its own pods → 0 endpoints.",
        stage=[
            ["kubectl", *_NS, "patch", "service/frontend", "--type=json", "-p",
             '[{"op":"replace","path":"/spec/selector","value":{"app":"frontend-broken-sre-eval"}}]'],
        ],
        revert=[
            ["kubectl", *_NS, "patch", "service/frontend", "--type=json", "-p",
             '[{"op":"replace","path":"/spec/selector","value":{"app":"frontend"}}]'],
        ],
        context=IncidentContext(
            namespace="boutique",
            workload="frontend",
            alert="frontend Service returns nothing / times out, but frontend pods show Running and Ready",
            skip_recall=True,
        ),
        wait_seconds=15,
        expect_categories={"networking", "config"},
        must_include_any=["selector", "endpoint", "0 endpoint", "no endpoint", "label", "service"],
    ),
    Incident(
        name="node_down",
        description="One worker node is cordoned and drained → its pods evicted and rescheduled.",
        stage=[
            ["kubectl", "cordon", "k3d-sre-lab-agent-1"],
            ["kubectl", "drain", "k3d-sre-lab-agent-1", "--ignore-daemonsets", "--delete-emptydir-data", "--force"],
        ],
        revert=[["kubectl", "uncordon", "k3d-sre-lab-agent-1"]],
        context=IncidentContext(
            namespace="boutique",
            workload=None,
            alert="multiple, unrelated boutique pods restarted/rescheduled at the same time",
            skip_recall=True,
        ),
        wait_seconds=40,
        expect_categories={"node"},
        must_include_any=["node", "cordon", "drain", "notready", "not ready", "evict"],
    ),
]
