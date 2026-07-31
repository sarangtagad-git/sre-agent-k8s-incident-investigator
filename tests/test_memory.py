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


def test_confirmed_outcome_carveout_language_present():
    # Added after a second live stress test: the first hardening (anti-corroboration)
    # over-corrected — the model stopped citing an "applied and approved by a human"
    # prior too, treating it the same as a repeated unverified guess. This carve-out
    # tells the model those are different in kind. See render_memory_digest's docstring.
    digest = render_memory_digest([_prior()])
    lowered = digest.lower()
    assert "different in kind" in lowered
    assert "confirmed real-world outcome" in lowered
    assert "legitimate evidence" in lowered
    # the anti-corroboration language for unverified repeats must still be present too
    assert "unverified" in lowered


def test_mixed_digest_keeps_both_instructions_and_labels_entries_correctly():
    # The realistic case both live stress tests actually exercised: a digest with
    # BOTH kinds of prior at once -- two unverified repeats of the same guess (should
    # stay uncited) and one human-confirmed outcome (should be usable). Guards against
    # a future edit to render_memory_digest() that fixes one instruction by weakening
    # or dropping the other -- see docs/memory-plan.md's two calibration findings.
    unverified_outcome = "proposed only — outcome unknown, never applied"
    confirmed_outcome = "applied and approved by a human — this fix was actually used"
    mixed = [
        _prior(
            when="2026-07-31T17:10:09",
            root_cause="revision 26 broken pod spec",
            outcome_label=unverified_outcome,
        ),
        _prior(
            when="2026-07-31T17:08:23",
            root_cause="revision 24 broken pod spec",
            outcome_label=confirmed_outcome,
        ),
        _prior(
            when="2026-07-31T04:43:58",
            root_cause="revision 22 broken pod spec",
            outcome_label=unverified_outcome,
        ),
    ]
    digest = render_memory_digest(mixed)
    lowered = digest.lower()

    # both instructions must coexist in the same digest
    assert "not independent confirmation" in lowered  # anti-corroboration (fix 1)
    assert "not itself new evidence" in lowered
    assert "different in kind" in lowered  # carve-out (fix 2)
    assert "legitimate evidence" in lowered

    # each entry's own outcome label must render distinctly and correctly -- an
    # unverified entry must never pick up the confirmed wording or vice versa
    assert digest.count(f"outcome: {unverified_outcome}") == 2
    assert digest.count(f"outcome: {confirmed_outcome}") == 1


def test_rejected_outcome_is_not_hidden():
    # A rejected/failed fix is useful memory too — decision 5 in memory-plan.md says
    # never hide a bad outcome.
    digest = render_memory_digest([_prior(outcome_label="proposed, but a human rejected this fix")])
    assert "rejected this fix" in digest
