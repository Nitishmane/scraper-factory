"""OpenTelemetry setup: traces, metrics, and logs to SigNoz.

All three signals go to the same OTLP endpoint. trace_id and span_id are injected into
log records automatically whenever a span is active, so logs correlate to traces for free.
"""
from __future__ import annotations

import atexit
import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "scraper-factory")
SIGNOZ_UI = os.getenv("SIGNOZ_UI_URL", "http://localhost:8080")

_initialised = False
_tracer: trace.Tracer | None = None
_meters: dict[str, object] = {}
# Held so shutdown() can force_flush all three signals -- the batch processors buffer,
# and a short-lived CLI process (`python -m factory ...`) exits before the next batch
# tick, dropping its logs/metrics unless we flush explicitly at exit.
_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None
_logger_provider: LoggerProvider | None = None


def init() -> None:
    """Idempotent. Safe to call from any entrypoint."""
    global _initialised, _tracer
    global _tracer_provider, _meter_provider, _logger_provider
    if _initialised:
        return

    resource = Resource.create({"service.name": SERVICE_NAME})

    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_tracer_provider)

    _meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    metrics.set_meter_provider(_meter_provider)

    _logger_provider = LoggerProvider(resource=resource)
    _logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    handler = LoggingHandler(level=logging.INFO, logger_provider=_logger_provider)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())  # keep terminal output for the demo

    _tracer = trace.get_tracer(SERVICE_NAME)
    _build_metrics()
    atexit.register(shutdown)
    _initialised = True


def shutdown() -> None:
    """Flush all three signals. Registered with atexit so CLI runs don't drop telemetry.

    Idempotent and never raises -- flushing must not turn a successful run into a crash.
    """
    for provider in (_tracer_provider, _meter_provider, _logger_provider):
        if provider is None:
            continue
        try:
            provider.force_flush()
        except Exception:  # a dead collector must not break process exit
            pass


def _build_metrics() -> None:
    m = metrics.get_meter(SERVICE_NAME)
    _meters.update(
        run_duration=m.create_histogram(
            "scraper.run.duration", unit="ms", description="Scrape run wall time"
        ),
        rows_returned=m.create_gauge(
            "scraper.rows_returned", description="Rows returned by a run"
        ),
        null_rate=m.create_gauge(
            "scraper.field.null_rate", description="Null rate across required fields"
        ),
        drift_detected=m.create_counter(
            "scraper.drift.detected", description="Drift detections"
        ),
        run_failures=m.create_counter(
            "scraper.run.failures",
            description="Scrape runs that raised (attrs target.provider, error.type)",
        ),
        heal_attempts=m.create_counter("scraper.heal.attempts", description="Heal attempts"),
        heal_success=m.create_counter("scraper.heal.success", description="Heals promoted"),
        verify_pass=m.create_counter("scraper.verify.pass", description="Fixture gate passes"),
        titles_considered=m.create_gauge(
            "ranker.titles_considered", description="Distinct upcoming titles ranked"
        ),
        catalog_match_rate=m.create_gauge(
            "ranker.catalog_match_rate",
            description="Share of the top-20 with a Philo record link",
        ),
        # --- richer per-operation signals -----------------------------------
        # Named to sit beside the histogram above (scraper.run.duration is the whole
        # run; scrape.fetch.duration is one page). Attrs: target.provider, fetch.mode.
        fetch_duration=m.create_histogram(
            "scrape.fetch.duration",
            unit="s",
            description="Wall time of one channel-page fetch+extract",
        ),
        rows_extracted=m.create_counter(
            "scrape.rows.extracted", description="Program rows extracted, by provider"
        ),
        pages_fetched=m.create_counter(
            "scrape.pages.fetched", description="Channel pages fetched, by provider/mode"
        ),
        relay_cache=m.create_counter(
            "relay.cache", description="Relay-mode fetch cache lookups (attr hit=true/false)"
        ),
        heal_events=m.create_counter(
            "heal.events",
            description="Heal terminal outcomes (attr outcome=repaired/escalated/rejected)",
        ),
        port_write_duration=m.create_histogram(
            "port.write.duration",
            unit="s",
            description="Port entity upsert latency (attrs blueprint, success)",
        ),
        tmdb_cache=m.create_counter(
            "tmdb.cache", description="TMDB enrichment cache lookups (attr hit=true/false)"
        ),
    )


def tracer() -> trace.Tracer:
    init()
    assert _tracer is not None
    return _tracer


def metric(name: str):
    init()
    return _meters[name]


def trace_url() -> str | None:
    """Deep link to the current trace in SigNoz. Stored on the Port scrape_run entity."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return f"{SIGNOZ_UI}/trace/{format(ctx.trace_id, '032x')}"


def log() -> logging.Logger:
    init()
    return logging.getLogger(SERVICE_NAME)
