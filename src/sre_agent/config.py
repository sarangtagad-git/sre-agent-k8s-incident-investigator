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

    # Gather-phase model swap: route ONLY the tool-calling loop through an
    # OpenAI-compatible open-source model; recall/correlate/hypothesize/propose stay
    # on Claude regardless. Empty gather_model = today's all-Claude behavior,
    # unchanged — this is an opt-in toggle, not a replacement.
    gather_model: str = ""  # e.g. "qwen/qwen-2.5-72b-instruct" (an OpenRouter model id)
    gather_base_url: str = "https://openrouter.ai/api/v1"
    gather_api_key: str = Field(default="")
    # Last-resort fallback $ per 1M tokens, used ONLY when gather_model/all_stages_model
    # isn't in graph.py's _GATHER_MODEL_PRICES table — real per-model prices live there
    # now (add a measured entry there instead of trusting this fallback). These two
    # defaults are still Qwen2.5-72B's August price; a real bug was found 2026-09-30
    # where DeepSeek runs silently used this stale Qwen number instead — kept here only
    # as the fallback, and graph.py now warns loudly whenever it's actually used.
    gather_price_in: float = 0.36
    gather_price_out: float = 0.40

    # All-stages model swap (config "C"): route correlate/hypothesize/propose through
    # an open model too, over the SAME OpenAI-compatible endpoint as gather_model
    # (gather_base_url/gather_api_key). Requires gather_model to also be set — these
    # three stages only ever see a plain-dict message transcript (the shape gather's
    # own open-model path produces) if gather itself already ran on an open model;
    # otherwise the transcript still holds real Claude SDK objects and translation
    # breaks. Empty = today's behavior, unchanged (these three stages stay on Claude
    # regardless of gather_model). No thinking/cache_control equivalent exists for
    # this path — see _analyze_call_open_model in graph.py.
    all_stages_model: str = ""  # e.g. "deepseek/deepseek-v4.1-flash" (an OpenRouter model id)

    # Optional LLM-observability export (see observability.py). Both are OTLP/HTTP
    # sinks fed from the SAME spans — set either, both, or neither; empty url = that
    # sink is skipped. Point at self-hosted instances (Apache-2.0 / MIT, free) for a
    # side-by-side comparison; nothing here changes agent behavior, only where the
    # trace of it goes.
    opik_url: str = ""  # e.g. http://localhost:5173
    opik_project_name: str = "sre-agent"

    langfuse_url: str = ""  # e.g. http://localhost:3000
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")

    # Read-only cluster access — the agent authenticates with THIS kubeconfig,
    # which is restricted to read-only by RBAC (see infra/rbac/).
    agent_kubeconfig: str = "infra/rbac/sre-agent.kubeconfig"

    # Metrics
    prometheus_url: str = "http://localhost:9090"

    # Where past investigate()/eval runs are persisted (Phase 7 history).
    history_db_path: str = "data/history.db"

    # Safety guardrail: hard cap on tool-calling iterations per investigation.
    agent_max_tool_iterations: int = 12

    # Phase 9: alert-triggered investigations (`sre-agent listen`). Autonomy is
    # propose-only and spend-bounded — see docs/alerts-plan.md.
    alert_namespaces: list[str] = ["boutique"]  # only auto-investigate these
    alert_daily_run_cap: int = 5  # max auto-investigations per calendar day
    alert_cooldown_minutes: int = 30  # per (namespace, alertname)
    alert_listen_port: int = 9095

    # Phase 11: verify an applied fix actually resolved the incident, instead of
    # trusting "the kubectl command didn't error" — see docs/verification-plan.md.
    verify_after_apply: bool = True
    verify_timeout_s: int = 90
    verify_poll_interval_s: int = 5
    verify_stability_checks: int = 3  # consecutive healthy polls required


def get_settings() -> Settings:
    """Return process settings. Kept as a function for easy test overrides."""
    return Settings()
