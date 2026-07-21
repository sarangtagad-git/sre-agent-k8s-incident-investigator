"""Phase 6: eval scoring logic. Pure — synthetic reports, no cluster or API key."""

from __future__ import annotations

from sre_agent.agent.schemas import RCAReport, Remediation
from sre_agent.evals import INCIDENTS, incident_passed, score

_CASCADE = next(i for i in INCIDENTS if i.name == "cascade")


def _report(**kw) -> RCAReport:
    base = dict(
        summary="redis-cart scaled to 0 so cartservice has no backend",
        root_cause="the redis-cart deployment was scaled to 0 replicas (desired=0)",
        category="dependency",
        confidence="high",
        confidence_score=0.9,
        impact="cart and checkout 500s",
        remediation=Remediation(
            action="scale redis-cart back up",
            command="kubectl -n boutique scale deployment/redis-cart --replicas=1",
            rationale="restore the backend",
        ),
    )
    base.update(kw)
    return RCAReport(**base)


def test_correct_cascade_report_passes():
    checks = score(_report(), _CASCADE)
    assert incident_passed(checks)
    assert all(c.passed for c in checks)  # info checks pass too here


def test_wrong_category_fails_critical():
    checks = score(_report(category="workload"), _CASCADE)
    assert not incident_passed(checks)
    cat = next(c for c in checks if c.name == "category")
    assert cat.critical and not cat.passed


def test_missing_required_substring_fails():
    # No "redis" anywhere → root_cause_match (critical) fails.
    r = _report(
        summary="a backend store was scaled to zero",
        root_cause="the store deployment has desired=0 replicas",
        evidence=[],
    )
    checks = score(r, _CASCADE)
    assert not incident_passed(checks)
    rc = next(c for c in checks if c.name == "root_cause_match")
    assert not rc.passed


def test_substring_can_match_in_evidence():
    r = _report(
        summary="a dependency is unavailable",
        root_cause="the backing store has no running pods",
        evidence=["get_workload_status: redis-cart desired=0 ready=0"],
    )
    checks = score(r, _CASCADE)
    assert incident_passed(checks)  # "redis" + "desired=0" found in evidence


def test_non_allowlisted_remediation_is_info_only():
    # A dangerous proposed fix fails the gate check but it's non-critical, so the
    # incident can still pass on the diagnosis — the gate check just flags it.
    r = _report(
        remediation=Remediation(
            action="delete it",
            command="kubectl -n boutique delete deployment redis-cart",
            rationale="nope",
        )
    )
    checks = score(r, _CASCADE)
    gate = next(c for c in checks if c.name == "remediation_gate_valid")
    assert not gate.passed and not gate.critical
    assert incident_passed(checks)  # critical checks still pass


def test_low_confidence_is_info_only():
    checks = score(_report(confidence="low", confidence_score=0.2), _CASCADE)
    conf = next(c for c in checks if c.name == "confidence")
    assert not conf.passed and not conf.critical
    assert incident_passed(checks)


def test_all_incidents_have_revert():
    for inc in INCIDENTS:
        assert inc.stage and inc.revert, inc.name
        assert all(a[0] == "kubectl" for a in inc.stage + inc.revert)
