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
]
