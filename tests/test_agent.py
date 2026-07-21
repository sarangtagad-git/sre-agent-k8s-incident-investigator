"""Phase 3 unit tests that need neither the cluster nor an API key."""

from __future__ import annotations

from sre_agent.agent.schemas import (
    Correlation,
    Hypothesis,
    Hypotheses,
    RCAReport,
    Remediation,
    TimelineEntry,
)
from sre_agent.agent.tools_bridge import ANTHROPIC_TOOLS, execute_tool


def test_tool_schemas_wellformed():
    names = {t["name"] for t in ANTHROPIC_TOOLS}
    assert names == {
        "get_workload_status",
        "get_pod_events",
        "get_pod_logs",
        "get_rollout_history",
        "query_prometheus",
    }
    for t in ANTHROPIC_TOOLS:
        assert t["description"]
        schema = t["input_schema"]
        assert schema["type"] == "object"
        assert "required" in schema
        # every required key is a declared property
        assert set(schema["required"]).issubset(schema["properties"].keys())


def test_execute_unknown_tool_is_graceful():
    content, is_error, record = execute_tool("nope", {}, clients=None)
    assert is_error is True
    assert record.ok is False


def test_rca_report_shape():
    r = RCAReport(
        summary="s",
        root_cause="rc",
        category="workload",
        confidence="high",
        impact="none",
        remediation=Remediation(action="a", command="kubectl ...", rationale="r"),
    )
    assert r.category == "workload"
    assert r.remediation.command.startswith("kubectl")
    # Phase 4 fields default so older call sites keep working.
    assert r.confidence_score == 0.0
    assert r.alternatives == []


def test_correlation_shape():
    c = Correlation(
        timeline=[TimelineEntry(when="rev 8", what="image changed to :bad")],
        dependency_chain=["frontend", "cartservice", "redis-cart"],
        what_changed="redis-cart scaled to 0",
        summary="cascade from redis outage",
    )
    assert c.dependency_chain[-1] == "redis-cart"  # leaf last
    assert c.timeline[0].when == "rev 8"


def test_hypotheses_rank_by_confidence():
    # The rank node's contract: highest confidence first.
    hs = Hypotheses(
        hypotheses=[
            Hypothesis(cause="downstream 5xx (symptom)", category="dependency", confidence=0.3),
            Hypothesis(cause="redis-cart is down", category="dependency", confidence=0.9),
            Hypothesis(cause="config drift", category="config", confidence=0.1),
        ]
    ).hypotheses
    ranked = sorted(hs, key=lambda h: h.confidence, reverse=True)
    assert ranked[0].cause == "redis-cart is down"
    assert [h.confidence for h in ranked] == [0.9, 0.3, 0.1]
