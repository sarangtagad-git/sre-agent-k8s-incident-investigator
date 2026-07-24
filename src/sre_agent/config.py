"""Central configuration, loaded from environment / .env (see .env.example)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    anthropic_api_key: str = Field(default="")
    agent_model: str = "claude-opus-4-8"  # set AGENT_MODEL=claude-sonnet-5 for cheaper runs
    agent_effort: str = "high"  # low | medium | high | xhigh | max

    # Read-only cluster access — the agent authenticates with THIS kubeconfig,
    # which is restricted to read-only by RBAC (see infra/rbac/).
    agent_kubeconfig: str = "infra/rbac/sre-agent.kubeconfig"

    # Metrics
    prometheus_url: str = "http://localhost:9090"

    # Where past investigate()/eval runs are persisted (Phase 7 history).
    history_db_path: str = "data/history.db"

    # Safety guardrail: hard cap on tool-calling iterations per investigation.
    agent_max_tool_iterations: int = 12


def get_settings() -> Settings:
    """Return process settings. Kept as a function for easy test overrides."""
    return Settings()
