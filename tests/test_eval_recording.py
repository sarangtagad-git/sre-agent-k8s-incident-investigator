"""Unit tests for save_recording/load_recording — pure serialization, no cluster or API key.

This is what step 6 (docs/incident-taxonomy-plan.md) actually is: proof that a RunResult
round-trips through a file losslessly, so `score()` can run against a saved recording
exactly as if it had just come back from a live `investigate()` call.
"""

from __future__ import annotations

from sre_agent.agent.schemas import RCAReport, Remediation, RunResult
from sre_agent.eval_recording import load_recording, save_recording


def _fake_result() -> RunResult:
    report = RCAReport(
        summary="currencyservice rollout broken",
        root_cause="bad image tag on the new revision",
        category="rollout",
        confidence="high",
        impact="currency conversion fails cluster-wide",
        remediation=Remediation(
            action="roll back",
            command="kubectl -n boutique rollout undo deployment/currencyservice",
            rationale="the previous revision was healthy",
        ),
    )
    return RunResult(report=report, input_tokens=100, output_tokens=50, cost_usd=0.02, duration_s=12.3)


def test_save_then_load_round_trips(tmp_path):
    original = _fake_result()
    path = save_recording("fake_incident", original, dir=tmp_path)
    assert path == tmp_path / "fake_incident.json"
    assert path.exists()

    loaded = load_recording("fake_incident", dir=tmp_path)
    assert loaded == original
    assert loaded.report.category == "rollout"
    assert loaded.cost_usd == 0.02


def test_load_missing_recording_raises_clear_error(tmp_path):
    try:
        load_recording("never_recorded", dir=tmp_path)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as e:
        assert "never_recorded" in str(e)
        assert "--record" in str(e)


def test_save_overwrites_prior_recording(tmp_path):
    save_recording("fake_incident", _fake_result(), dir=tmp_path)
    updated = _fake_result()
    updated.cost_usd = 0.99
    save_recording("fake_incident", updated, dir=tmp_path)

    loaded = load_recording("fake_incident", dir=tmp_path)
    assert loaded.cost_usd == 0.99
