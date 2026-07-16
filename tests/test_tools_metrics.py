"""Integration tests for query_prometheus (needs Prometheus reachable).

Skips automatically if Prometheus isn't up / not port-forwarded.
"""

from __future__ import annotations

import pytest

from sre_agent.tools import PrometheusResult, query_prometheus


@pytest.fixture(scope="module")
def _prom_up():
    r = query_prometheus("up")
    if r.error:
        pytest.skip(f"Prometheus not reachable: {r.error}")
    return r


def test_query_up_returns_vector(_prom_up):
    r = _prom_up
    assert isinstance(r, PrometheusResult)
    assert r.result_type == "vector"
    assert len(r.samples) > 0
    # 'up' is 1 for healthy targets
    assert any(s.value == 1.0 for s in r.samples)


def test_scalar_query():
    r = query_prometheus("vector(42)")
    if r.error:
        pytest.skip(f"Prometheus not reachable: {r.error}")
    assert r.samples and r.samples[0].value == 42.0
