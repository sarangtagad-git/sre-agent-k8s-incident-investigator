"""Integration tests for the read-only tools — run against the live k3d cluster.

They SKIP automatically if the cluster/kubeconfig isn't reachable, so `make test`
still passes in CI or offline.
"""

from __future__ import annotations

import pytest

from sre_agent.tools import (
    DeploymentStatus,
    NamespaceWorkloadStatus,
    PodEvent,
    get_pod_events,
    get_workload_status,
)


@pytest.fixture(scope="module")
def clients():
    from sre_agent.k8s import load_readonly_clients

    try:
        c = load_readonly_clients()
        c["core"].list_namespaced_pod("boutique", limit=1)  # connectivity + permission probe
        return c
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"cluster/kubeconfig not available: {e}")


def test_get_workload_status_shape(clients):
    ws = get_workload_status("boutique", clients=clients)
    assert isinstance(ws, NamespaceWorkloadStatus)
    assert ws.namespace == "boutique"
    assert len(ws.pods) > 0
    assert all(isinstance(d, DeploymentStatus) for d in ws.deployments)
    assert "frontend" in {d.name for d in ws.deployments}


def test_get_workload_status_selector(clients):
    ws = get_workload_status("boutique", selector="app=frontend", clients=clients)
    assert all(p.name.startswith("frontend") for p in ws.pods)


def test_get_pod_events_shape(clients):
    ws = get_workload_status("boutique", selector="app=frontend", clients=clients)
    pod = ws.pods[0].name
    evs = get_pod_events("boutique", pod, clients=clients)
    assert isinstance(evs, list)
    assert all(isinstance(e, PodEvent) for e in evs)
