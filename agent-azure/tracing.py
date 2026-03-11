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

    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")

    if conn_str:
        # configure_azure_monitor sets up traces + logs + metrics in one call,
        # routing all three signals to Application Insights. Python's logging
        # module is automatically bridged to OpenTelemetry Logs.
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor(
            connection_string=conn_str,
            resource=Resource({
                "service.name": "agent-azure-kb",
                "service.version": "1.0.0",
            }),
        )
        logger.info("Azure Monitor telemetry enabled (traces + logs → Application Insights)")
    else:
        # No Azure Monitor — export traces to console only
        resource = Resource({
            "service.name": "agent-azure-kb",
            "service.version": "1.0.0",
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)

    _tracer = trace.get_tracer("agent-azure.kb-agent")
    return _tracer


def get_tracer() -> trace.Tracer:
    if _tracer is None:
        return setup_tracing()
    return _tracer
