"""Phase 9: alert trigger policy. Pure — synthetic payloads, no cluster or API key."""

from __future__ import annotations

from datetime import datetime

import pytest

from sre_agent.alerts import Alert, WebhookPayload, should_investigate, to_incident_context

NOW = datetime(2026, 7, 25, 12, 0, 0)
POLICY = dict(namespaces=["boutique"], daily_cap=5, cooldown_minutes=30)


def _alert(**kw) -> Alert:
    base = dict(
        status="firing",
        labels={"alertname": "BoutiquePodCrashLooping", "namespace": "boutique", "pod": "emailservice-abc"},
        annotations={"summary": "pod is crash looping"},
    )
    base.update(kw)
    return Alert(**base)


def _run(started_at: str, alert: str = "BoutiquePodCrashLooping: ...", namespace: str = "boutique",
         triggered_by: str = "alert") -> dict:
    return {"started_at": started_at, "namespace": namespace, "alert": alert, "triggered_by": triggered_by}


# --- payload parsing -------------------------------------------------------


def test_payload_parses_real_shape():
    payload = WebhookPayload.model_validate(
        {
            "version": "4",
            "status": "firing",
            "receiver": "sre-agent",
            "groupLabels": {"alertname": "X"},
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "X", "namespace": "boutique"},
                    "annotations": {"summary": "s"},
                    "startsAt": "2026-07-25T11:58:00.000Z",
                    "fingerprint": "abc123",
                }
            ],
        }
    )
    assert len(payload.alerts) == 1
    assert payload.alerts[0].alertname == "X"


def test_context_mapping_uses_deployment_label_and_pod_in_text():
    a = _alert(labels={"alertname": "KubeDeploymentReplicasMismatch", "namespace": "boutique",
                       "deployment": "redis-cart"})
    ctx = to_incident_context(a)
    assert ctx.namespace == "boutique"
    assert ctx.workload == "redis-cart"
    assert ctx.alert.startswith("KubeDeploymentReplicasMismatch:")

    b = _alert()  # pod label, no deployment label
    ctx = to_incident_context(b)
    assert ctx.workload is None  # no name-mangling heuristics — agent locates it
    assert "emailservice-abc" in ctx.alert


def test_context_requires_namespace():
    with pytest.raises(ValueError):
        to_incident_context(_alert(labels={"alertname": "X"}))


# --- policy guardrails, first failure wins ---------------------------------


def test_firing_in_allowlisted_namespace_goes():
    d = should_investigate(_alert(), now=NOW, recent_runs=[], **POLICY)
    assert d.go


def test_resolved_alert_skipped():
    d = should_investigate(_alert(status="resolved"), now=NOW, recent_runs=[], **POLICY)
    assert not d.go and "not firing" in d.reason


def test_missing_namespace_skipped():
    d = should_investigate(_alert(labels={"alertname": "X"}), now=NOW, recent_runs=[], **POLICY)
    assert not d.go and "no namespace" in d.reason


def test_non_allowlisted_namespace_skipped():
    a = _alert(labels={"alertname": "X", "namespace": "kube-system"})
    d = should_investigate(a, now=NOW, recent_runs=[], **POLICY)
    assert not d.go and "allowlist" in d.reason


def test_cooldown_blocks_recent_same_alert():
    runs = [_run("2026-07-25T11:45:00")]  # 15 min ago, same alertname+namespace
    d = should_investigate(_alert(), now=NOW, recent_runs=runs, **POLICY)
    assert not d.go and "cooldown" in d.reason


def test_cooldown_counts_manual_runs_too():
    runs = [_run("2026-07-25T11:50:00", triggered_by="manual")]
    d = should_investigate(_alert(), now=NOW, recent_runs=runs, **POLICY)
    assert not d.go and "cooldown" in d.reason


def test_cooldown_expires():
    runs = [_run("2026-07-25T11:15:00")]  # 45 min ago > 30 min window
    d = should_investigate(_alert(), now=NOW, recent_runs=runs, **POLICY)
    assert d.go


def test_cooldown_is_per_alertname_and_namespace():
    runs = [
        _run("2026-07-25T11:55:00", alert="SomeOtherAlert: ..."),
        _run("2026-07-25T11:55:00", namespace="other-ns"),
    ]
    d = should_investigate(_alert(), now=NOW, recent_runs=runs, **POLICY)
    assert d.go


def test_daily_cap_counts_only_todays_auto_runs():
    runs = [
        # five auto runs today for OTHER alerts -> cap reached
        _run(f"2026-07-25T0{h}:00:00", alert=f"Other{h}: ...") for h in range(1, 6)
    ]
    d = should_investigate(_alert(), now=NOW, recent_runs=runs, **POLICY)
    assert not d.go and "daily cap" in d.reason

    # same five runs but manual -> cap untouched
    manual = [dict(r, triggered_by="manual") for r in runs]
    assert should_investigate(_alert(), now=NOW, recent_runs=manual, **POLICY).go

    # and yesterday's auto runs don't count
    yesterday = [dict(r, started_at=r["started_at"].replace("-25T", "-24T")) for r in runs]
    assert should_investigate(_alert(), now=NOW, recent_runs=yesterday, **POLICY).go
