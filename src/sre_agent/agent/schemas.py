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


# The incident taxonomy, shared by the report and each hypothesis so ranking is apples-to-apples.
Category = Literal[
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


class TimelineEntry(BaseModel):
    """One event on the incident timeline (correlate node)."""

    when: str  # timestamp or relative ("~5m ago", "revision 8") — as seen in evidence
    what: str  # what happened, grounded in a tool result


class Correlation(BaseModel):
    """The correlate node's output: how the evidence fits together in time and topology."""

    timeline: list[TimelineEntry] = Field(default_factory=list)
    # Service dependency chain from the alerting entrypoint to the failing leaf (leaf LAST),
    # e.g. ["frontend", "cartservice", "redis-cart"]. Empty for non-cascade incidents.
    dependency_chain: list[str] = Field(default_factory=list)
    what_changed: str  # the trigger — the change/event that most plausibly started this
    summary: str


class Hypothesis(BaseModel):
    """One candidate root cause with a confidence score (hypothesize node)."""

    cause: str
    category: Category
    confidence: float  # 0.0–1.0 — how well the evidence supports THIS cause
    supporting: list[str] = Field(default_factory=list)  # evidence for
    against: list[str] = Field(default_factory=list)  # evidence against / caveats


class Hypotheses(BaseModel):
    """Wrapper so hypothesize can emit a list via structured output."""

    hypotheses: list[Hypothesis] = Field(default_factory=list)


class RCAReport(BaseModel):
    """The agent's structured root-cause analysis (its final output)."""

    summary: str
    root_cause: str
    category: Category
    confidence: Literal["high", "medium", "low"]  # human-readable band
    confidence_score: float = 0.0  # 0.0–1.0, from the top-ranked hypothesis
    evidence: list[str] = Field(default_factory=list)
    # Other causes considered and rejected, each with its score, e.g. "config drift (0.15): …".
    alternatives: list[str] = Field(default_factory=list)
    impact: str
    remediation: Remediation


class RunResult(BaseModel):
    """Everything one `investigate()` call produced — the report plus the trail
    behind it (evidence, correlation, ranked hypotheses, cost) — so a caller can
    persist the full run, not just the final RCA."""

    report: RCAReport
    evidence: list[ToolRecord] = Field(default_factory=list)
    correlation: Correlation | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)  # ranked, highest first
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0


class AgentState(TypedDict):
    """LangGraph state flowing through gather -> correlate -> hypothesize -> rank -> propose."""

    incident: IncidentContext
    messages: list  # Anthropic conversation (content blocks preserved)
    evidence: list[ToolRecord]
    iterations: int
    correlation: Correlation | None
    hypotheses: list[Hypothesis]  # ranked (highest confidence first) after the rank node
    report: RCAReport | None
