"""
OpenTelemetry instrumentation setup for p2.

Configures traces, metrics, and log correlation via the OpenTelemetry SDK.
Called from ASGI application startup before the Django app handles requests.
"""
from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider


def setup_telemetry() -> None:
    """Initialise OpenTelemetry SDK: traces, metrics, and log correlation."""
    import os
    from django.conf import settings

    # Skip if OTel is disabled or no endpoint configured
    if os.getenv('OTEL_SDK_DISABLED', '').lower() in ('true', '1', 'yes'):
        return
    endpoint = settings.OTEL_ENDPOINT
    if not endpoint or endpoint == 'http://localhost:4317':
        return

    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.OTEL_ENDPOINT))
    )
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=settings.OTEL_ENDPOINT)
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    DjangoInstrumentor().instrument()
    LoggingInstrumentor().instrument(set_logging_format=True)


tracer = trace.get_tracer("p2")
meter = metrics.get_meter("p2")

s3_request_counter = meter.create_counter("p2.s3.requests", description="S3 API request count")
s3_latency_histogram = meter.create_histogram("p2.s3.latency", description="S3 API latency", unit="ms")
storage_op_histogram = meter.create_histogram("p2.storage.op_latency", description="Storage operation latency", unit="ms")
