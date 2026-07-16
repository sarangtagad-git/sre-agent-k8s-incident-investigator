"""State + output shapes for the investigation agent."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


class IncidentContext(BaseModel):
    """What the agent is asked to investigate (from an alert or the CLI)."""

    namespace: str
    workload: str | None = None  # e.g. a deployment/pod name, if known
    alert: str | None = None  # the alert text / symptom that triggered us


class ToolRecord(BaseModel):
    """One tool call the agent made (for the evidence trail + OTel)."""

    tool: str
    input: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    summary: str = ""  # short human-readable note


class Remediation(BaseModel):
    """A PROPOSED fix — never executed by the agent (Phase 5 gate)."""

    action: str  # e.g. "rollout undo", "scale redis-cart=1"
    command: str  # the exact kubectl command a human would run
    rationale: str
    reversible: bool = True


class RCAReport(BaseModel):
    """The agent's structured root-cause analysis (its final output)."""

    summary: str
    root_cause: str
    category: Literal[
        "workload",
        "config",
        "scheduling",
        "rollout",
        "networking",
        "dependency",
        "storage",
        "node",
        "saturation",
        "unknown",
    ]
    confidence: Literal["high", "medium", "low"]
    evidence: list[str] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list)
    impact: str
    remediation: Remediation


class AgentState(TypedDict):
    """LangGraph state that flows through gather -> report."""

    incident: IncidentContext
    messages: list  # Anthropic conversation (content blocks preserved)
    evidence: list[ToolRecord]
    iterations: int
    report: RCAReport | None
