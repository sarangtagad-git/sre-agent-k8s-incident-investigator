"""get_network_policies — the agent's equivalent of `kubectl describe networkpolicy`.

Fills a real blind spot: two pods can both be Ready, both pass every other check,
and traffic between them is still silently dropped because a NetworkPolicy's rules
don't admit it. Nothing else in the tool surface can see this.
"""

from __future__ import annotations

import json

from ..k8s import load_readonly_clients
from ..observability import get_tracer, truncate_for_trace as _truncate
from .schemas import NetworkPolicyInfo, NetworkPolicyRule

_tracer = get_tracer()


def _selector_str(selector) -> str:
    """Render a LabelSelector as a short human-readable string."""
    if selector is None:
        return "<all>"
    labels = selector.match_labels or {}
    parts = [f"{k}={v}" for k, v in labels.items()]
    if selector.match_expressions:
        parts.append(f"{len(selector.match_expressions)} expression(s)")
    return ",".join(parts) if parts else "<all>"


def _peer_str(peer) -> str:
    if peer.pod_selector is not None:
        return f"pods where {_selector_str(peer.pod_selector)}"
    if peer.namespace_selector is not None:
        return f"namespaces where {_selector_str(peer.namespace_selector)}"
    if peer.ip_block is not None:
        return f"ipBlock {peer.ip_block.cidr}"
    return "<unspecified peer>"


def _port_str(port) -> str:
    proto = port.protocol or "TCP"
    return f"{proto}/{port.port}" if port.port is not None else proto


def _rule(direction: str, peers, ports) -> NetworkPolicyRule:
    return NetworkPolicyRule(
        direction=direction,
        peers=[_peer_str(p) for p in (peers or [])],
        ports=[_port_str(p) for p in (ports or [])],
    )


def get_network_policies(
    namespace: str,
    clients: dict | None = None,
) -> list[NetworkPolicyInfo]:
    """Return every NetworkPolicy in a namespace, with rules rendered for reasoning.

    Read-only: uses list (get/list/watch). An empty pod_selector means "applies to
    every pod in the namespace" — the most common way a policy accidentally blocks
    more than intended.
    """
    clients = clients or load_readonly_clients()
    with _tracer.start_as_current_span("tool.get_network_policies") as span:
        span.set_attribute("k8s.namespace", namespace)
        span.set_attribute("gen_ai.tool.call.arguments", json.dumps({"namespace": namespace}))
        raw = clients["networking"].list_namespaced_network_policy(namespace).items

        policies: list[NetworkPolicyInfo] = []
        for np in raw:
            rules: list[NetworkPolicyRule] = []
            for ing in np.spec.ingress or []:
                rules.append(_rule("ingress", ing.from_, ing.ports))
            for eg in np.spec.egress or []:
                rules.append(_rule("egress", eg.to, eg.ports))
            policies.append(
                NetworkPolicyInfo(
                    name=np.metadata.name,
                    namespace=namespace,
                    pod_selector=_selector_str(np.spec.pod_selector),
                    policy_types=list(np.spec.policy_types or []),
                    rules=rules,
                    created=np.metadata.creation_timestamp,
                )
            )
        span.set_attribute("result.policy_count", len(policies))
        span.set_attribute(
            "gen_ai.tool.call.result",
            _truncate(json.dumps([p.model_dump(mode="json") for p in policies])),
        )
        return policies
