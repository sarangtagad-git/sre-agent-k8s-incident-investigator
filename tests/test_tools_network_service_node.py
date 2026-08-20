"""Integration tests for get_network_policies, get_service_status, get_node_status
(live k3d cluster). Skip automatically if the cluster/kubeconfig isn't reachable.

These back the Batch-1 incident taxonomy gap check (docs/incident-taxonomy-plan.md):
network_blocked, wrong_service_selector, node_down.
"""

from __future__ import annotations

import pytest

from sre_agent.tools import (
    NetworkPolicyInfo,
    NodeStatus,
    ServiceStatus,
    get_network_policies,
    get_node_status,
    get_service_status,
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


def test_get_network_policies_shape(clients):
    # boutique ships with no NetworkPolicies today — this asserts the tool works
    # cleanly against that (empty list, not an error), same as `kubectl get netpol`.
    policies = get_network_policies("boutique", clients=clients)
    assert isinstance(policies, list)
    assert all(isinstance(p, NetworkPolicyInfo) for p in policies)


def test_get_service_status_frontend(clients):
    svc = get_service_status("boutique", "frontend", clients=clients)
    assert isinstance(svc, ServiceStatus)
    assert svc.name == "frontend"
    assert svc.selector  # frontend's Service has a real selector
    # frontend has running pods today, so it should have at least one ready endpoint.
    assert svc.endpoint_count >= 1
    assert svc.ready_endpoint_count <= svc.endpoint_count


def test_get_node_status_shape(clients):
    nodes = get_node_status(clients=clients)
    assert isinstance(nodes, list)
    assert len(nodes) >= 1
    assert all(isinstance(n, NodeStatus) for n in nodes)
    # every node reports at least a Ready condition either way
    assert all(any(c.startswith("Ready=") for c in n.conditions) for n in nodes)
