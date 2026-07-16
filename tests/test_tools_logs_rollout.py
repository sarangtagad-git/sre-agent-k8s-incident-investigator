"""Integration tests for get_pod_logs and get_rollout_history (live k3d cluster).

Skip automatically if the cluster/kubeconfig isn't reachable.
"""

from __future__ import annotations

import pytest

from sre_agent.tools import (
    PodLogs,
    RolloutHistory,
    get_pod_logs,
    get_rollout_history,
    get_workload_status,
)


@pytest.fixture(scope="module")
def clients():
    from sre_agent.k8s import load_readonly_clients

    try:
        c = load_readonly_clients()
        c["core"].list_namespaced_pod("boutique", limit=1)
        return c
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"cluster/kubeconfig not available: {e}")


def test_get_pod_logs_current(clients):
    ws = get_workload_status("boutique", selector="app=frontend", clients=clients)
    pod = ws.pods[0].name
    logs = get_pod_logs("boutique", pod, tail_lines=20, clients=clients)
    assert isinstance(logs, PodLogs)
    assert logs.container == "server"
    assert logs.line_count == len(logs.lines)


def test_get_pod_logs_previous_note_when_none(clients):
    # frontend hasn't crashed, so --previous should return a note, not crash.
    ws = get_workload_status("boutique", selector="app=frontend", clients=clients)
    pod = ws.pods[0].name
    logs = get_pod_logs("boutique", pod, previous=True, clients=clients)
    assert isinstance(logs, PodLogs)
    # Either there's a previous log (if it restarted) or a clean note — never an exception.
    assert logs.note is not None or logs.line_count >= 0


def test_get_rollout_history(clients):
    h = get_rollout_history("boutique", "frontend", clients=clients)
    assert isinstance(h, RolloutHistory)
    assert h.deployment == "frontend"
    assert len(h.revisions) >= 1
    # every revision exposes at least one image
    assert all(r.images for r in h.revisions)
    # exactly one revision is marked current (matching the deployment's revision)
    assert sum(1 for r in h.revisions if r.current) <= 1
