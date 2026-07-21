"""Phase 5: the remediation safety gate. Pure validation — no cluster or API key needed."""

from __future__ import annotations

import pytest

from sre_agent.remediation import validate_remediation


def test_allows_scale():
    d = validate_remediation("kubectl -n boutique scale deployment redis-cart --replicas=1")
    assert d.allowed
    assert d.mutating == [["kubectl", "-n", "boutique", "scale", "deployment", "redis-cart", "--replicas=1"]]
    assert d.readonly == []


def test_allows_rollout_undo():
    d = validate_remediation("kubectl -n boutique rollout undo deployment/emailservice --to-revision=7")
    assert d.allowed
    assert d.mutating and d.mutating[0][3:5] == ["rollout", "undo"]


def test_allows_rollout_restart():
    assert validate_remediation("kubectl -n boutique rollout restart deploy/frontend").allowed


def test_allows_mutating_then_readonly_verify_chain():
    # The agent often proposes a fix && a read-only verification step.
    d = validate_remediation(
        "kubectl -n boutique scale deploy/redis-cart --replicas=1 "
        "&& kubectl -n boutique get endpoints redis-cart"
    )
    assert d.allowed
    assert len(d.mutating) == 1 and len(d.readonly) == 1
    assert d.readonly[0][3] == "get"


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl -n boutique delete deployment redis-cart",   # destructive verb
        "kubectl delete ns boutique",                          # destructive verb
        "kubectl -n boutique apply -f manifest.yaml",          # apply
        "kubectl -n boutique patch deploy/x --type=json -p=[]",# patch
        "kubectl -n boutique exec pod/x -- sh",                # exec
        "kubectl -n boutique edit deploy/x",                   # edit
        "helm upgrade boutique ./chart",                       # not kubectl
    ],
)
def test_rejects_non_allowlisted_verbs(cmd):
    d = validate_remediation(cmd)
    assert not d.allowed
    assert d.mutating == [] and d.readonly == []


def test_rejects_protected_namespace():
    d = validate_remediation("kubectl -n kube-system scale deploy/coredns --replicas=0")
    assert not d.allowed
    assert "kube-system" in d.reason


def test_rejects_kubeconfig_redirect():
    d = validate_remediation("kubectl -n boutique scale deploy/x --replicas=1 --kubeconfig=/tmp/evil")
    assert not d.allowed
    assert "redirect" in d.reason


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl -n boutique scale deploy/x --replicas=1; rm -rf /",   # ;
        "kubectl -n boutique scale deploy/x --replicas=1 | tee out",   # |
        "kubectl -n boutique scale deploy/x --replicas=$(whoami)",     # $()
        "kubectl -n boutique scale deploy/x --replicas=1 > /etc/x",    # >
    ],
)
def test_rejects_shell_metacharacters(cmd):
    d = validate_remediation(cmd)
    assert not d.allowed
    assert "metacharacter" in d.reason


def test_rejects_readonly_only_command():
    # A command with no mutating action is not a remediation.
    d = validate_remediation("kubectl -n boutique get pods")
    assert not d.allowed
    assert "mutating" in d.reason


def test_rejects_empty():
    assert not validate_remediation("").allowed
    assert not validate_remediation("   ").allowed
