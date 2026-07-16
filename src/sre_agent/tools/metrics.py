"""query_prometheus — the agent's metrics lens (Prometheus HTTP API via PromQL).

This is the "golden signals" tool: error rates, latency, saturation — the view
that catches cascades where no pod looks unhealthy. Unlike the other tools it
speaks HTTP (httpx) to Prometheus, not the Kubernetes API. Errors are captured in
the `error` field, never raised, so a missing port-forward can't crash a run.

Requires Prometheus reachable at settings.prometheus_url, e.g.:
  kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090
"""

from __future__ import annotations

import httpx

from ..config import get_settings
from ..observability import get_tracer
from .schemas import MetricSample, MetricSeries, PrometheusRangeResult, PrometheusResult

_tracer = get_tracer()


def query_prometheus(
    query: str,
    time: float | str | None = None,
    base_url: str | None = None,
    timeout: float = 10.0,
) -> PrometheusResult:
    """Run an instant PromQL query and return typed samples."""
    base = (base_url or get_settings().prometheus_url).rstrip("/")
    with _tracer.start_as_current_span("tool.query_prometheus") as span:
        span.set_attribute("prometheus.query", query)
        params: dict = {"query": query}
        if time is not None:
            params["time"] = time
        try:
            resp = httpx.get(f"{base}/api/v1/query", params=params, timeout=timeout)
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — connection/JSON errors -> captured
            return PrometheusResult(query=query, error=f"request failed: {exc}")

        if payload.get("status") != "success":
            return PrometheusResult(query=query, error=payload.get("error", "query failed"))

        data = payload["data"]
        rtype = data.get("resultType", "")
        samples: list[MetricSample] = []
        if rtype == "vector":
            for item in data.get("result", []):
                ts, val = item["value"]
                samples.append(
                    MetricSample(labels=item.get("metric", {}), value=float(val), timestamp=float(ts))
                )
        elif rtype == "scalar":
            ts, val = data["result"]
            samples.append(MetricSample(labels={}, value=float(val), timestamp=float(ts)))

        span.set_attribute("result.sample_count", len(samples))
        return PrometheusResult(query=query, result_type=rtype, samples=samples)


def query_prometheus_range(
    query: str,
    start: float | str,
    end: float | str,
    step: str = "30s",
    base_url: str | None = None,
    timeout: float = 15.0,
) -> PrometheusRangeResult:
    """Run a range PromQL query (time series) — for correlating change over time."""
    base = (base_url or get_settings().prometheus_url).rstrip("/")
    with _tracer.start_as_current_span("tool.query_prometheus_range") as span:
        span.set_attribute("prometheus.query", query)
        params = {"query": query, "start": start, "end": end, "step": step}
        try:
            resp = httpx.get(f"{base}/api/v1/query_range", params=params, timeout=timeout)
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return PrometheusRangeResult(query=query, error=f"request failed: {exc}")

        if payload.get("status") != "success":
            return PrometheusRangeResult(query=query, error=payload.get("error", "query failed"))

        series = [
            MetricSeries(
                labels=item.get("metric", {}),
                values=[(float(t), float(v)) for t, v in item.get("values", [])],
            )
            for item in payload["data"].get("result", [])
        ]
        span.set_attribute("result.series_count", len(series))
        return PrometheusRangeResult(query=query, series=series)
