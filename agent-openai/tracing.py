"""
tracing.py - OpenTelemetry tracing setup for agent-openai.

Exports traces to the console by default. Set OTLP_ENDPOINT to route to a
collector (e.g. Jaeger, Grafana Tempo, or any OTLP-compatible backend).

Usage:
    from tracing import setup_tracing, get_tracer
    tracer = setup_tracing()
    with tracer.start_as_current_span("my.span") as span:
        span.set_attribute("key", "value")
"""

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = logging.getLogger(__name__)

_tracer: trace.Tracer | None = None

_RESOURCE = Resource({"service.name": "agent-openai", "service.version": "1.0.0"})


def setup_tracing() -> trace.Tracer:
    global _tracer
    if _tracer is not None:
        return _tracer

    provider = TracerProvider(resource=_RESOURCE)

    otlp_endpoint = os.environ.get("OTLP_ENDPOINT")
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("OTLP tracing enabled → %s", otlp_endpoint)
    elif os.environ.get("TRACING", "true").lower() != "false":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("agent-openai")
    return _tracer


def get_tracer() -> trace.Tracer:
    if _tracer is None:
        return setup_tracing()
    return _tracer
