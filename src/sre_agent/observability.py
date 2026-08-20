"""OpenTelemetry tracing skeleton — 'observe the observer'.

Every investigation and tool call becomes a span, so we can debug the agent like
any other production service (latency, token cost, failures). setup_tracing() is
called once from agent.graph.investigate() — the single choke point every caller
(CLI investigate/eval, listener.py) already goes through.

Sinks are additive and independently opt-in via Settings (see config.py):
  - console (always on)                     — for local `--verbose` debugging.
  - Opik (OPIK_URL)                         — self-hosted, Apache-2.0.
  - Langfuse (LANGFUSE_URL + keys)          — self-hosted, MIT core.
Both are OTLP/HTTP backends fed from the exact same spans, so running both is just
two more BatchSpanProcessors on the same provider — no instrumentation forking,
no duplicated code at any `_tracer.start_as_current_span(...)` call site.
"""

from __future__ import annotations

import base64

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_configured = False


def setup_tracing(service_name: str = "sre-agent", exporter=None, settings=None) -> None:
    """Idempotently configure a global tracer provider.

    `exporter` overrides the default console sink (used by tests). `settings`
    defaults to get_settings() — accepted as a param only to avoid a hard import
    cycle for callers that already have one in hand.
    """
    global _configured
    if _configured:
        return
    if settings is None:
        from .config import get_settings

        settings = get_settings()

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter or ConsoleSpanExporter()))

    if settings.opik_url:
        opik_exporter = OTLPSpanExporter(
            endpoint=f"{settings.opik_url.rstrip('/')}/api/v1/private/otel/v1/traces",
            headers={"projectName": settings.opik_project_name},
        )
        provider.add_span_processor(BatchSpanProcessor(opik_exporter))

    if settings.langfuse_url:
        auth = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
        ).decode()
        langfuse_exporter = OTLPSpanExporter(
            endpoint=f"{settings.langfuse_url.rstrip('/')}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"},
        )
        provider.add_span_processor(BatchSpanProcessor(langfuse_exporter))

    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer(name: str = "sre-agent"):
    return trace.get_tracer(name)


TRACE_TEXT_LIMIT = 4000  # plenty to read a call back in Opik/Langfuse without ballooning span size


def truncate_for_trace(text: str, limit: int = TRACE_TEXT_LIMIT) -> str:
    """Cap a string before attaching it to a span. Shared so every call site — LLM
    calls in graph.py, tool calls in tools_bridge.py and every tools/*.py module —
    applies the exact same size policy instead of duplicating (or forgetting) it."""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"… [truncated, {len(text)} chars total]"
