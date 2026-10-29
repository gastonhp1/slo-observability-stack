"""OpenTelemetry setup: traces and logs over OTLP, JSON logs on stdout.

Metrics deliberately stay on the Prometheus client (see ``metrics.py``): the
SLO rules depend on exact series names, and a pull model keeps ``up`` as a
reliable "is the target alive" signal.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import Settings

LOGGER_NAME = "shop"


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with trace/span ids when a span is active."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        span_ctx = trace.get_current_span().get_span_context()
        if span_ctx.is_valid:
            payload["trace_id"] = format(span_ctx.trace_id, "032x")
            payload["span_id"] = format(span_ctx.span_id, "016x")
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def build_resource(settings: Settings) -> Resource:
    return Resource.create(
        {
            "service.name": settings.service,
            "service.version": settings.version,
            "service.namespace": "shop",
            "deployment.environment.name": settings.environment,
        }
    )


def setup_logging(settings: Settings, resource: Resource) -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(JsonFormatter())
    root.addHandler(stdout)

    if settings.otlp_endpoint:
        # Imported lazily: the logs SDK is still marked experimental upstream.
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=settings.otlp_endpoint, insecure=True))
        )
        set_logger_provider(provider)
        root.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))

    # Access logs are emitted by our own middleware with route/status/duration.
    logging.getLogger("uvicorn.access").disabled = True
    return logging.getLogger(LOGGER_NAME)


def setup_tracing(settings: Settings, resource: Resource) -> TracerProvider:
    provider = TracerProvider(resource=resource)
    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True))
        )
    trace.set_tracer_provider(provider)
    return provider
