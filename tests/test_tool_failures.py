"""Tool failures must be visible, not silent (eval check, prompt note, guaranteed disclosure).

Pure — synthetic records/reports, no cluster or API key. The motivating case: a crashed
get_network_policies (`'V1NetworkPolicyIngressRule' object has no attribute 'from_'`) left
the agent to conclude "saturation" with normal confidence, and the only trace of the crash
was a mark on the run-detail page.
"""

from __future__ import annotations

import json

import pytest

from sre_agent.agent import tools_bridge
from sre_agent.agent.prompts import (
    merge_evidence_gaps,
    render_tool_failure_note,
    tool_gap_lines,
)
from sre_agent.agent.schemas import RCAReport, Remediation, ToolRecord
from sre_agent.evals import INCIDENTS, incident_passed, score
from sre_agent.tools.schemas import PrometheusResult

_CASCADE = next(i for i in INCIDENTS if i.name == "cascade")

CRASH = ToolRecord(
    tool="get_network_policies",
    ok=False,
    summary="error: 'V1NetworkPolicyIngressRule' object has no attribute 'from_'",
)
PROM_DOWN = ToolRecord(
    tool="query_prometheus",
    ok=True,
    summary="request failed: [Errno 111] Connection refused",
    warning="request failed: [Errno 111] Connection refused",
)
HEALTHY = ToolRecord(tool="get_workload_status", ok=True, summary="12 pods, 12 deployments")


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


def _by_name(checks):
    return {c.name: c for c in checks}


# --- ToolRecord.problem ---------------------------------------------------------------

def test_problem_classifies_crash_warning_and_healthy():
    assert CRASH.problem() == ("error", CRASH.summary)
    assert PROM_DOWN.problem() == ("warning", "request failed: [Errno 111] Connection refused")
    assert HEALTHY.problem() is None


def test_problem_recovers_prometheus_failure_from_records_saved_before_the_warning_field():
    # Recordings/history rows written before `warning` existed have ok=True and only the
    # summary — they must still be gradable, or replaying old recordings would hide the gap.
    legacy = ToolRecord(tool="query_prometheus", ok=True, summary="request failed: [Errno 111]")
    assert legacy.warning is None
    assert legacy.problem() == ("warning", "request failed: [Errno 111]")


def test_a_healthy_prometheus_query_is_not_a_problem():
    ok = ToolRecord(tool="query_prometheus", ok=True, summary="3 samples")
    assert ok.problem() is None


# --- eval checks ----------------------------------------------------------------------

def test_tool_crash_fails_the_incident_even_when_the_answer_is_right():
    checks = score(_report(), _CASCADE, evidence=[HEALTHY, CRASH])
    by = _by_name(checks)
    assert not by["no_tool_errors"].passed
    assert by["no_tool_errors"].critical
    assert "get_network_policies" in by["no_tool_errors"].detail
    assert "from_" in by["no_tool_errors"].detail
    assert not incident_passed(checks)  # right category + root cause, still fails


def test_a_crash_in_an_unrelated_tool_still_fails_the_incident():
    # Option A (by cause): a crash is a bug in our tool code whether or not this incident
    # needed that tool — the eval doubles as a test of the tools themselves.
    unrelated = ToolRecord(tool="get_node_status", ok=False, summary="error: boom")
    assert not incident_passed(score(_report(), _CASCADE, evidence=[unrelated]))


def test_prometheus_unreachable_is_only_a_warning_and_does_not_fail_the_incident():
    checks = score(_report(), _CASCADE, evidence=[HEALTHY, PROM_DOWN])
    by = _by_name(checks)
    assert by["no_tool_errors"].passed
    assert not by["no_tool_warnings"].passed
    assert not by["no_tool_warnings"].critical
    assert "query_prometheus" in by["no_tool_warnings"].detail
    assert incident_passed(checks)


def test_repeated_prometheus_failures_are_reported_once():
    checks = score(_report(), _CASCADE, evidence=[PROM_DOWN, PROM_DOWN, PROM_DOWN])
    detail = _by_name(checks)["no_tool_warnings"].detail
    assert detail.count("query_prometheus") == 1


def test_clean_run_passes_both_tool_checks():
    checks = score(_report(), _CASCADE, evidence=[HEALTHY, HEALTHY])
    by = _by_name(checks)
    assert by["no_tool_errors"].passed and by["no_tool_warnings"].passed
    assert incident_passed(checks)


def test_tool_checks_are_skipped_when_no_evidence_is_given():
    # Existing callers that only pass (report, incident) keep the exact old check list.
    names = {c.name for c in score(_report(), _CASCADE)}
    assert "no_tool_errors" not in names and "no_tool_warnings" not in names


# --- prompt note + guaranteed disclosure ---------------------------------------------

def test_note_is_empty_when_every_tool_worked():
    assert render_tool_failure_note([HEALTHY, HEALTHY]) == ""


def test_note_names_failed_tools_and_says_missing_is_not_all_clear():
    note = render_tool_failure_note([HEALTHY, CRASH, PROM_DOWN])
    assert "get_network_policies" in note and "FAILED" in note
    assert "query_prometheus" in note and "returned no data" in note
    assert "MISSING" in note


def test_gap_lines_dedupe_repeated_failures():
    assert len(tool_gap_lines([PROM_DOWN, PROM_DOWN, PROM_DOWN])) == 1


def test_disclosure_is_added_when_the_model_ignores_the_prompt():
    merged = merge_evidence_gaps([], [HEALTHY, CRASH])
    assert len(merged) == 1 and "get_network_policies" in merged[0]


def test_disclosure_does_not_duplicate_a_gap_the_model_already_named():
    named = ["get_network_policies failed, so no policy rules were checked"]
    assert merge_evidence_gaps(named, [CRASH]) == named


def test_disclosure_is_a_no_op_on_a_healthy_run():
    assert merge_evidence_gaps([], [HEALTHY]) == []


def test_report_instruction_defines_evidence_gaps_even_on_a_healthy_run():
    # Live-found: the compact schema sketch drops field descriptions, so the model saw only
    # `"evidence_gaps": ["string"]` and, on a run where EVERY tool worked, filled it with
    # checks it had chosen not to run ("node status not checked"). The definition has to be
    # in the always-on instruction, not just a code comment or a failure-only note.
    from sre_agent.agent.prompts import REPORT_INSTRUCTION

    assert "evidence_gaps" in REPORT_INSTRUCTION
    assert "chose not to run" in REPORT_INSTRUCTION


# --- dispatcher: Prometheus failure is a warning, not a green check ------------------

def test_dispatcher_records_a_prometheus_failure_as_a_warning_and_flags_it_to_the_model(monkeypatch):
    monkeypatch.setattr(
        tools_bridge,
        "query_prometheus",
        lambda q: PrometheusResult(query=q, error="request failed: [Errno 111] Connection refused"),
    )
    content, is_error, record = tools_bridge.execute_tool("query_prometheus", {"query": "up"}, None)
    assert is_error is True
    assert record.ok is True
    assert record.warning == "request failed: [Errno 111] Connection refused"
    assert record.problem() == ("warning", record.warning)
    assert "Connection refused" in json.loads(content)["error"]


def test_dispatcher_leaves_a_healthy_prometheus_query_alone(monkeypatch):
    monkeypatch.setattr(tools_bridge, "query_prometheus", lambda q: PrometheusResult(query=q))
    _, is_error, record = tools_bridge.execute_tool("query_prometheus", {"query": "up"}, None)
    assert is_error is False
    assert record.warning is None and record.problem() is None


@pytest.mark.parametrize("bad", [CRASH, PROM_DOWN])
def test_records_round_trip_through_json_with_their_warning(bad):
    # Recordings and history rows are JSON — the new field must survive a save/load.
    assert ToolRecord.model_validate_json(bad.model_dump_json()).problem() == bad.problem()
