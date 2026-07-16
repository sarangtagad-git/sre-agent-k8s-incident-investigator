"""Load a READ-ONLY Kubernetes client from the agent's restricted kubeconfig.

The agent must NEVER use your admin kubeconfig. It authenticates as the
`sre-agent` ServiceAccount (see infra/rbac/), which RBAC restricts to
get/list/watch. Any write call will be rejected by the API server.
"""

from __future__ import annotations

from pathlib import Path

from kubernetes import client, config as k8s_config

from .config import get_settings


def load_readonly_clients() -> dict:
    """Return typed API clients bound to the read-only kubeconfig."""
    settings = get_settings()
    kubeconfig = Path(settings.agent_kubeconfig)
    if not kubeconfig.exists():
        raise FileNotFoundError(
            f"Read-only kubeconfig not found at '{kubeconfig}'. "
            "Generate it with:  bash infra/rbac/gen-kubeconfig.sh"
        )
    k8s_config.load_kube_config(config_file=str(kubeconfig))
    return {
        "core": client.CoreV1Api(),
        "apps": client.AppsV1Api(),
    }
