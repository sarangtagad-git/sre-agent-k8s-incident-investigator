"""Phase 1 smoke tests — package imports and config load without external deps."""

from sre_agent import __version__
from sre_agent.config import get_settings


def test_version():
    assert __version__ == "0.1.0"


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = get_settings()
    assert s.agent_model.startswith("claude")
    assert s.agent_max_tool_iterations > 0
    assert s.agent_kubeconfig.endswith(".kubeconfig")
