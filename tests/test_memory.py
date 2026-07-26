"""Phase 10: incident memory — the pure digest formatter. No DB, no LLM, no cluster."""

from __future__ import annotations

from sre_agent.agent.prompts import render_memory_digest
from sre_agent.agent.schemas import PriorIncident


def _prior(**kw) -> PriorIncident:
    base = dict(
        run_id="abc123def456",
        when="2026-07-24T20:07:00",
        category="dependency",
        confidence_score=0.92,
        root_cause="redis-cart deployment was scaled to 0 replicas",
        remediation_command="kubectl -n boutique scale deployment/redis-cart --replicas=1",
        outcome_label="applied and approved by a human — this fix was actually used",
    )
    base.update(kw)
    return PriorIncident(**base)


def test_empty_list_produces_empty_string():
    # No filler text like "no prior incidents found" should ever reach the model.
    assert render_memory_digest([]) == ""


def test_single_prior_incident_includes_all_fields():
    digest = render_memory_digest([_prior()])
    assert "redis-cart deployment was scaled to 0 replicas" in digest
    assert "0.92" in digest
    assert "dependency" in digest
    assert "kubectl -n boutique scale deployment/redis-cart --replicas=1" in digest
    assert "applied and approved by a human" in digest
    assert "2026-07-24T20:07:00" in digest


def test_hedging_language_present_so_the_model_does_not_treat_it_as_fact():
    digest = render_memory_digest([_prior()])
    assert "context only" in digest.lower()
    assert "ground your actual conclusion" in digest.lower()


def test_anti_false_corroboration_language_present():
    # Added after a live stress test showed the model describing 3 near-identical,
    # closely-timed priors as "multiple independent confirmations" — this instruction
    # heads that off explicitly. See render_memory_digest's docstring.
    digest = render_memory_digest([_prior(), _prior(when="2026-07-24T20:10:00")])
    lowered = digest.lower()
    assert "not independent confirmation" in lowered
    assert "today's fresh evidence" in lowered
    assert "not itself new evidence" in lowered


def test_missing_confidence_score_renders_as_unknown_not_a_crash():
    digest = render_memory_digest([_prior(confidence_score=None)])
    assert "confidence ?" in digest


def test_missing_category_renders_as_unknown():
    digest = render_memory_digest([_prior(category=None)])
    assert "unknown" in digest


def test_multiple_prior_incidents_all_appear():
    prior = [
        _prior(root_cause="first incident's root cause", when="2026-07-01T00:00:00"),
        _prior(root_cause="second incident's root cause", when="2026-07-15T00:00:00"),
    ]
    digest = render_memory_digest(prior)
    assert "first incident's root cause" in digest
    assert "second incident's root cause" in digest


def test_rejected_outcome_is_not_hidden():
    # A rejected/failed fix is useful memory too — decision 5 in memory-plan.md says
    # never hide a bad outcome.
    digest = render_memory_digest([_prior(outcome_label="proposed, but a human rejected this fix")])
    assert "rejected this fix" in digest
