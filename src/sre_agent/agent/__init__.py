"""The investigation agent (Phase 3+).

A LangGraph state machine: gather (ReAct tool loop) -> report (structured RCA).
Claude drives tool selection over the read-only Phase-2 tools; the proposed
remediation must pass the human approval gate (Phase 5) before anything runs.
"""

from .schemas import IncidentContext, RCAReport, Remediation

__all__ = ["IncidentContext", "RCAReport", "Remediation", "investigate"]


def investigate(incident: IncidentContext) -> RCAReport:
    """Lazy wrapper so importing the package doesn't require anthropic/langgraph."""
    from .graph import investigate as _investigate

    return _investigate(incident)
