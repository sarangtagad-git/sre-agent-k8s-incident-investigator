"""Phase 9: alert-triggered investigations — webhook payload models + trigger policy.

The policy core (`should_investigate`) is a pure function over data passed in (the
alert, the clock, recent history rows), so every guardrail is unit-testable without a
cluster, an API key, or time mocking. `evaluate()` is the thin wiring that feeds it
real settings + history.

Safety posture (see docs/alerts-plan.md): alert-triggered runs are propose-mode only —
this module decides *whether to look*, never whether to change anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .agent.schemas import IncidentContext

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"  # matches history_store's started_at format


class Alert(BaseModel):
    """One alert inside an Alertmanager v4 webhook payload (fields we use only)."""

    model_config = ConfigDict(extra="ignore")

    status: str = "firing"  # "firing" | "resolved"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    fingerprint: str = ""

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "unknown-alert")

    @property
    def namespace(self) -> str | None:
        return self.labels.get("namespace")


class WebhookPayload(BaseModel):
    """The envelope Alertmanager POSTs to a webhook receiver."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    alerts: list[Alert] = Field(default_factory=list)


def to_incident_context(alert: Alert) -> IncidentContext:
    """Map an alert onto the same IncidentContext the CLI builds.

    workload comes only from an explicit `deployment` label (KubeDeployment* alerts
    carry one). Pod-level alerts pass the pod name inside the alert text instead —
    locating the owning workload is the agent's job, not a name-mangling heuristic's.
    """
    if alert.namespace is None:
        raise ValueError("alert has no namespace label")
    desc = alert.annotations.get("summary") or alert.annotations.get("description") or ""
    pod = alert.labels.get("pod")
    text = f"{alert.alertname}: {desc}" if desc else alert.alertname
    if pod and pod not in text:
        text += f" (pod {pod})"
    return IncidentContext(
        namespace=alert.namespace,
        workload=alert.labels.get("deployment"),
        alert=text,
    )


@dataclass(frozen=True)
class Decision:
    go: bool
    reason: str  # why we're investigating, or why we declined — always logged


def should_investigate(
    alert: Alert,
    *,
    now: datetime,
    recent_runs: Iterable[Mapping[str, Any]],
    namespaces: list[str],
    daily_cap: int,
    cooldown_minutes: int,
) -> Decision:
    """Apply the guardrails in order; first failure wins.

    `recent_runs` are history rows (any mapping with started_at / namespace / alert /
    triggered_by) covering at least the current day and the cooldown window.
    """
    if alert.status != "firing":
        return Decision(False, f"status is {alert.status!r}, not firing")
    if alert.namespace is None:
        return Decision(False, "no namespace label on the alert")
    if alert.namespace not in namespaces:
        return Decision(False, f"namespace {alert.namespace!r} not in allowlist {namespaces}")

    cooldown_floor = now - timedelta(minutes=cooldown_minutes)
    todays_auto_runs = 0
    for run in recent_runs:
        started = datetime.strptime(run["started_at"], _TS_FORMAT)
        if run["triggered_by"] == "alert" and started.date() == now.date():
            todays_auto_runs += 1
        # Cooldown counts ANY run for this (namespace, alertname) — if a human just
        # investigated the same alert, re-running it automatically helps nobody.
        if (
            started >= cooldown_floor
            and run["namespace"] == alert.namespace
            and (run["alert"] or "").startswith(alert.alertname)
        ):
            return Decision(
                False,
                f"cooldown: a run for {alert.alertname!r} in {alert.namespace!r} "
                f"started at {run['started_at']} (window {cooldown_minutes}m)",
            )

    if todays_auto_runs >= daily_cap:
        return Decision(False, f"daily cap reached ({todays_auto_runs}/{daily_cap} auto runs today)")

    return Decision(True, f"firing {alert.alertname!r} in allowlisted namespace {alert.namespace!r}")


def evaluate(alert: Alert, now: datetime | None = None) -> Decision:
    """Wire the pure policy to real settings + history. Used by the listener."""
    from . import history_store
    from .config import get_settings

    settings = get_settings()
    now = now or datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cooldown_floor = now - timedelta(minutes=settings.alert_cooldown_minutes)
    since = min(day_start, cooldown_floor)
    return should_investigate(
        alert,
        now=now,
        recent_runs=history_store.runs_since(since.strftime(_TS_FORMAT)),
        namespaces=settings.alert_namespaces,
        daily_cap=settings.alert_daily_run_cap,
        cooldown_minutes=settings.alert_cooldown_minutes,
    )
