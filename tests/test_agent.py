"""Phase 3 unit tests that need neither the cluster nor an API key."""

from __future__ import annotations

from sre_agent.agent.schemas import RCAReport, Remediation
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
