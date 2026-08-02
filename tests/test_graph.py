"""Phase 10/11: the recall node's row -> PriorIncident mapping, especially the
outcome-label taxonomy. Pure — a plain dict stands in for a sqlite3.Row (both support
`row["col"]` access), no DB or LLM needed."""

from __future__ import annotations

import json

from sre_agent.agent.graph import _row_to_prior_incident


def _row(**kw) -> dict:
    base = dict(
        id="abc123def456",
        started_at="2026-07-31T17:08:23",
        category="rollout",
        confidence_score=0.82,
        root_cause="revision 24 broken pod spec",
        mode="execute",
        approval_status="approved_applied",
        verification_status=None,
        report_json=json.dumps({"remediation": {"command": "kubectl rollout undo deployment/emailservice"}}),
    )
    base.update(kw)
    return base


def test_propose_mode_is_always_unknown_outcome_regardless_of_other_fields():
    row = _row(mode="propose", approval_status="n/a", verification_status="confirmed_healthy")
    p = _row_to_prior_incident(row)
    assert p.outcome_label == "proposed only — outcome unknown, never applied"


def test_rejected_by_human():
    row = _row(approval_status="rejected")
    assert _row_to_prior_incident(row).outcome_label == "proposed, but a human rejected this fix"


def test_blocked_dry_run_failed_apply_failed_all_read_as_safety_gate_failure():
    for status in ("blocked", "dry_run_failed", "apply_failed"):
        row = _row(approval_status=status)
        assert _row_to_prior_incident(row).outcome_label == "proposed, but this fix failed the safety gate"


def test_approved_applied_confirmed_healthy_says_verified_not_permanently_confirmed():
    row = _row(verification_status="confirmed_healthy")
    label = _row_to_prior_incident(row).outcome_label
    assert "verified healthy immediately after applying" in label
    # must not overclaim permanence -- see docs/verification-plan.md decision 3
    assert "confirmed to have resolved" not in label
    assert "permanent" not in label or "not a permanent guarantee" in label


def test_approved_applied_still_unhealthy_is_a_directive_warning():
    row = _row(verification_status="still_unhealthy")
    label = _row_to_prior_incident(row).outcome_label
    assert "did NOT resolve" in label
    assert "do not propose this same fix again" in label.lower()


def test_approved_applied_not_checked_falls_back_to_unverified_wording():
    row = _row(verification_status="not_checked")
    label = _row_to_prior_incident(row).outcome_label
    assert label == (
        "applied and approved by a human (not independently verified whether it "
        "resolved the issue)"
    )


def test_approved_applied_null_verification_also_falls_back_to_unverified_wording():
    # NULL verification_status covers both pre-Phase-11 rows and verification
    # disabled via Settings -- either way, an honest "we don't know", never a guess.
    row = _row(verification_status=None)
    label = _row_to_prior_incident(row).outcome_label
    assert "not independently verified" in label


def test_remediation_command_extracted_from_report_json():
    row = _row()
    p = _row_to_prior_incident(row)
    assert p.remediation_command == "kubectl rollout undo deployment/emailservice"


def test_malformed_report_json_falls_back_to_empty_command_not_a_crash():
    row = _row(report_json="not valid json")
    p = _row_to_prior_incident(row)
    assert p.remediation_command == ""
