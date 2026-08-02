"""Phase 11: applied-fix verification. Pure — synthetic workload statuses, no cluster,
no API key, no real sleeping (clock/sleep are injected)."""

from __future__ import annotations

from sre_agent.remediation import is_workload_healthy, verify_recovery
from sre_agent.tools.schemas import DeploymentStatus, NamespaceWorkloadStatus


def _status(ready: int, desired: int, unavailable: int, name: str = "emailservice") -> NamespaceWorkloadStatus:
    return NamespaceWorkloadStatus(
        namespace="boutique",
        deployments=[
            DeploymentStatus(
                name=name, namespace="boutique", desired=desired, ready=ready,
                available=ready, updated=ready, unavailable=unavailable,
            )
        ],
    )


# --- is_workload_healthy: pure signal check ---------------------------------


def test_healthy_when_ready_equals_desired_and_no_unavailable():
    assert is_workload_healthy(_status(1, 1, 0), "emailservice") is True


def test_unhealthy_when_ready_below_desired():
    assert is_workload_healthy(_status(0, 1, 0), "emailservice") is False


def test_unhealthy_when_unavailable_nonzero_even_if_ready_matches():
    # The exact pattern seen live all session: an old healthy pod masks a new
    # crash-looping one, so ready can look fine while unavailable is not zero.
    assert is_workload_healthy(_status(1, 1, 1), "emailservice") is False


def test_workload_not_found_returns_none_not_a_guess():
    assert is_workload_healthy(_status(1, 1, 0, name="cartservice"), "emailservice") is None


# --- verify_recovery: the stability-window poller ----------------------------


class _Clock:
    """Deterministic clock/sleep pair so tests never sleep for real."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _ScriptedChecks:
    """Returns one NamespaceWorkloadStatus per call from a fixed script, holding on
    the last entry once exhausted (so a "never recovers" test can run past the
    timeout without running out of scripted values)."""

    def __init__(self, healthy_sequence: list[bool]) -> None:
        self.statuses = [_status(1, 1, 0) if h else _status(0, 1, 1) for h in healthy_sequence]
        self.calls = 0

    def __call__(self) -> NamespaceWorkloadStatus:
        idx = min(self.calls, len(self.statuses) - 1)
        self.calls += 1
        return self.statuses[idx]


def test_confirmed_healthy_requires_multiple_consecutive_checks_not_just_one():
    # This is the direct regression test for the flapping-health review finding:
    # verify_recovery must NOT short-circuit on the first healthy reading.
    clock = _Clock()
    checks = _ScriptedChecks([True, True, True, True])  # stays healthy throughout
    result = verify_recovery(
        "emailservice", checks,
        timeout_s=90, poll_interval_s=5, stability_checks=3,
        sleep_fn=clock.sleep, clock_fn=clock.now,
    )
    assert result.status == "confirmed_healthy"
    assert checks.calls == 3  # exactly stability_checks calls, not 1


def test_heals_then_breaks_again_resets_the_counter_and_does_not_confirm_early():
    # The exact flapping scenario: healthy, healthy, then unhealthy BEFORE the
    # stability window completes. Must not short-circuit to confirmed_healthy.
    clock = _Clock()
    # healthy, healthy, UNHEALTHY (resets), healthy, healthy, healthy -> confirms here
    checks = _ScriptedChecks([True, True, False, True, True, True])
    result = verify_recovery(
        "emailservice", checks,
        timeout_s=90, poll_interval_s=5, stability_checks=3,
        sleep_fn=clock.sleep, clock_fn=clock.now,
    )
    assert result.status == "confirmed_healthy"
    assert checks.calls == 6  # had to restart the 3-in-a-row count after the blip


def test_never_healthy_reports_still_unhealthy_after_timeout():
    clock = _Clock()
    checks = _ScriptedChecks([False] * 30)  # never recovers
    result = verify_recovery(
        "emailservice", checks,
        timeout_s=90, poll_interval_s=5, stability_checks=3,
        sleep_fn=clock.sleep, clock_fn=clock.now,
    )
    assert result.status == "still_unhealthy"
    assert "90s" in result.detail


def test_flaps_forever_never_reaching_stability_reports_still_unhealthy():
    # Alternating healthy/unhealthy forever never accumulates 3 in a row.
    clock = _Clock()
    checks = _ScriptedChecks([True, False] * 30)
    result = verify_recovery(
        "emailservice", checks,
        timeout_s=90, poll_interval_s=5, stability_checks=3,
        sleep_fn=clock.sleep, clock_fn=clock.now,
    )
    assert result.status == "still_unhealthy"


def test_no_workload_named_skips_verification_without_calling_check_fn():
    clock = _Clock()
    checks = _ScriptedChecks([True, True, True])
    result = verify_recovery(
        None, checks,
        timeout_s=90, poll_interval_s=5, stability_checks=3,
        sleep_fn=clock.sleep, clock_fn=clock.now,
    )
    assert result.status == "not_checked"
    assert checks.calls == 0  # never even tried


def test_workload_missing_from_status_reports_not_checked_immediately():
    clock = _Clock()

    def missing_check() -> NamespaceWorkloadStatus:
        return _status(1, 1, 0, name="some-other-deployment")

    result = verify_recovery(
        "emailservice", missing_check,
        timeout_s=90, poll_interval_s=5, stability_checks=3,
        sleep_fn=clock.sleep, clock_fn=clock.now,
    )
    assert result.status == "not_checked"
    assert "not found" in result.detail
