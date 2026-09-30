"""ALL_STAGES_MODEL config guard (config "C" — see graph.py module docstring).

correlate/hypothesize/propose only ever see a translatable (plain-dict) transcript
if gather itself already produced one, so ALL_STAGES_MODEL without GATHER_MODEL is
a misconfiguration, not a valid fourth mode — investigate() must refuse it before
touching the cluster or the Anthropic API, the same way the existing
GATHER_MODEL/GATHER_API_KEY pairing is guarded. Pure — no cluster, no API key, no
live model call needed."""

from __future__ import annotations

import pytest

from sre_agent.agent.graph import investigate
from sre_agent.agent.schemas import IncidentContext


def test_all_stages_model_without_gather_model_is_rejected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("GATHER_MODEL", "")
    monkeypatch.setenv("ALL_STAGES_MODEL", "deepseek/deepseek-v4.1-flash")
    incident = IncidentContext(namespace="boutique", workload="frontend", alert="test")
    with pytest.raises(RuntimeError, match="ALL_STAGES_MODEL is set but GATHER_MODEL is not"):
        investigate(incident)
