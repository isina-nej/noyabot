"""Optional OTLP metric and log exporters.

The Lightweight profile has zero telemetry transport dependencies. The Full
profile uses OpenTelemetry SDK exporters with bounded batch/periodic delivery.
Only *redacted structured* Noya events are sent to the OTLP log exporter.
"""
from __future__ import annotations

import logging
import os
import threading

_LOCK = threading.Lock()
_STATE = {"initialized": False, "meter": None, "metrics": False, "logs": False}
_COUNTERS = {}
_HISTOGRAMS = {}


def _signal_url(signal: str):
    endpoint = os.getenv("NOYA_OTEL_EXPORTER_OTLP_HTTP_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        return ""
    for suffix in ("/v1/traces", "/v1/metrics", "/v1/logs"):
        if endpoint.endswith(suffix):
            return endpoint[:-len(suffix)] + "/v1/" + signal
    return endpoint + "/v1/" + signal


def configure():
    """One-time best-effort exporter wiring."""
    with _LOCK:
        if _STATE["initialized"]:
            return
        _STATE["initialized"] = True
    metrics_url = _signal_url("metrics")
    if not metrics_url:
        return
    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        resource = Resource.create({
            "service.name": os.getenv("NOYA_SERVICE_NAME", "noyabot"),
            "deployment.environment": os.getenv("NOYA_ENVIRONMENT", "development"),
        })
        exporter = OTLPMetricExporter(endpoint=metrics_url, timeout=4)
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15000, export_timeout_millis=5000)
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        otel_metrics.set_meter_provider(provider)
        _STATE["meter"] = otel_metrics.get_meter("botapp.noya")
        _STATE["metrics"] = True
    except Exception:
        # External collector unavailability or missing packages must not break bot.
        pass

    try:
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=_signal_url("logs"), timeout=4),
            max_queue_size=1024, max_export_batch_size=128, schedule_delay_millis=2000,
        ))
        handler = LoggingHandler(logger_provider=provider, level=logging.INFO)
        logger = logging.getLogger("botapp.observability")
        # Don't attach to the root or botapp loggers: arbitrary legacy message
        # bodies must never be exported to a third-party observability backend.
        if not any(isinstance(item, LoggingHandler) for item in logger.handlers):
            logger.addHandler(handler)
        _STATE["logs"] = True
    except Exception:
        pass


def status():
    return {"metrics": _STATE["metrics"], "logs": _STATE["logs"]}


def _metric(name: str, kind: str):
    meter = _STATE["meter"]
    if meter is None:
        return None
    cache = _COUNTERS if kind == "counter" else _HISTOGRAMS
    with _LOCK:
        instrument = cache.get(name)
        if instrument is None:
            if kind == "counter":
                instrument = meter.create_counter(name)
            else:
                instrument = meter.create_histogram(name, unit="ms")
            cache[name] = instrument
        return instrument


def counter(name: str, amount: float, labels: dict):
    try:
        instrument = _metric(name, "counter")
        if instrument is not None:
            instrument.add(amount, attributes=labels)
    except Exception:
        pass


def histogram(name: str, value: float, labels: dict):
    try:
        instrument = _metric(name, "histogram")
        if instrument is not None:
            instrument.record(value, attributes=labels)
    except Exception:
        pass
