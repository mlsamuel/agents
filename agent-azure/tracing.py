import os
import logging
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter

logger = logging.getLogger(__name__)

_tracer: trace.Tracer | None = None


def setup_tracing() -> trace.Tracer:
    global _tracer
    if _tracer is not None:
        return _tracer

    resource = Resource({
        "service.name": "agent-azure-kb",
        "service.version": "1.0.0",
    })
    provider = TracerProvider(resource=resource)

    # Always export to console so spans are visible during demos
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    # If Application Insights connection string is set, also export to Azure Monitor
    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if conn_str:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
            azure_exporter = AzureMonitorTraceExporter(connection_string=conn_str)
            provider.add_span_processor(SimpleSpanProcessor(azure_exporter))
            logger.info("Azure Monitor tracing enabled")
        except ImportError:
            logger.warning("azure-monitor-opentelemetry not installed; skipping Azure Monitor export")

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("agent-azure.kb-agent")
    return _tracer


def get_tracer() -> trace.Tracer:
    if _tracer is None:
        return setup_tracing()
    return _tracer
