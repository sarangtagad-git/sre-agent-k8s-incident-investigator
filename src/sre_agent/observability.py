"""OpenTelemetry tracing skeleton — 'observe the observer'.

Every investigation and tool call will become a span, so we can debug the agent
like any other production service (latency, token cost, failures). Wired in from
day 1; exporters get swapped for OTLP -> Grafana/Tempo later.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_configured = False


def setup_tracing(service_name: str = "sre-agent", exporter=None) -> None:
    """Idempotently configure a global tracer provider (console exporter by default)."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter or ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer(name: str = "sre-agent"):
    return trace.get_tracer(name)
