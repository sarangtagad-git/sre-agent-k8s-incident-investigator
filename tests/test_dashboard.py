"""Phase 11: dashboard status pills/badges. Pure — a plain dict stands in for a
sqlite3.Row (both support `row["col"]` access), no DB, no cluster, no browser."""

from __future__ import annotations

from sre_agent.dashboard import _remediation_status, _status_pill


def _row(**kw) -> dict:
    base = dict(
        mode="execute",
        approval_status="approved_applied",
        verification_status=None,
        verification_detail=None,
        resolved=None,
    )
    base.update(kw)
    return base


# --- _status_pill: feed card + detail header --------------------------------


def test_propose_mode_pill():
    assert _status_pill(_row(mode="propose", approval_status="n/a")) == ("warn", "proposed")


def test_eval_pass_and_fail_pills():
    assert _status_pill(_row(mode="eval", approval_status="n/a", resolved=1)) == ("good", "pass")
    assert _status_pill(_row(mode="eval", approval_status="n/a", resolved=0)) == ("bad", "fail")


def test_execute_rejected_blocked_dry_run_apply_failed_pills():
    assert _status_pill(_row(approval_status="rejected")) == ("warn", "rejected")
    assert _status_pill(_row(approval_status="blocked")) == ("bad", "blocked")
    assert _status_pill(_row(approval_status="dry_run_failed")) == ("bad", "dry-run failed")
    assert _status_pill(_row(approval_status="apply_failed")) == ("bad", "apply failed")


def test_approved_applied_confirmed_healthy_pill():
    row = _row(verification_status="confirmed_healthy")
    assert _status_pill(row) == ("good", "applied and verified healthy")


def test_approved_applied_still_unhealthy_pill():
    # The case with no live example currently in data/history.db (the only real one
    # was a manually-fabricated test row, since deleted) -- covered here so the
    # behavior is provable without needing to plant test data in real history again.
    row = _row(verification_status="still_unhealthy")
    assert _status_pill(row) == ("bad", "applied and verified unhealthy")


def test_approved_applied_not_checked_or_null_falls_back_to_plain_applied():
    assert _status_pill(_row(verification_status="not_checked")) == ("warn", "applied")
    assert _status_pill(_row(verification_status=None)) == ("warn", "applied")


# --- _remediation_status: the "Proposed remediation" card -------------------


def test_propose_mode_remediation_status():
    cls, label, _ = _remediation_status(_row(mode="propose", approval_status="n/a"))
    assert (cls, label) == ("warn", "Proposed only")


def test_eval_remediation_status():
    cls, label, _ = _remediation_status(_row(mode="eval", approval_status="n/a", resolved=1))
    assert (cls, label) == ("good", "Validated & reverted")
    cls, label, _ = _remediation_status(_row(mode="eval", approval_status="n/a", resolved=0))
    assert (cls, label) == ("bad", "Validated & reverted")


def test_confirmed_healthy_remediation_status_includes_point_in_time_caveat():
    cls, label, explanation = _remediation_status(_row(
        verification_status="confirmed_healthy",
        verification_detail="emailservice: healthy for 3 consecutive checks (~10s)",
    ))
    assert cls == "good"
    assert label == "Applied & verified healthy"
    assert "healthy for 3 consecutive checks" in explanation
    assert "not a permanent guarantee" in explanation


def test_still_unhealthy_remediation_status_is_red_and_says_failed():
    cls, label, explanation = _remediation_status(_row(
        verification_status="still_unhealthy",
        verification_detail="emailservice: still unhealthy after 90s (19 checks, needed 3 consecutive)",
    ))
    assert cls == "bad"
    assert "verification FAILED" in label
    assert "still unhealthy after 90s" in explanation


def test_not_checked_or_null_remediation_status_says_unverified():
    for vstatus in ("not_checked", None):
        cls, label, explanation = _remediation_status(_row(verification_status=vstatus))
        assert cls == "warn"
        assert label == "Applied (not verified)"
        assert "not independently verified" in explanation


def test_rejected_blocked_dry_run_apply_failed_remediation_statuses():
    assert _remediation_status(_row(approval_status="rejected"))[:2] == ("warn", "Rejected by human")
    assert _remediation_status(_row(approval_status="blocked"))[:2] == ("bad", "Blocked by safety gate")
    assert _remediation_status(_row(approval_status="dry_run_failed"))[:2] == ("bad", "Dry-run failed")
    assert _remediation_status(_row(approval_status="apply_failed"))[:2] == ("bad", "Apply failed")
