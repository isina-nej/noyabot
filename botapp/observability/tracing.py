"""Optional OpenTelemetry backend abstraction (modular, no hard dependency).

If the ``opentelemetry`` SDK is not installed — or the OTLP endpoint fails —
every function here no-ops or degrades to correlation-id bridging, and the
application keeps running (telemetry never breaks the bot).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("botapp.observability.tracing")

_STATE = {"initialized": False, "tracer": None, "provider": "none"}


def trace_provider() -> str:
    """Active trace provider id (``none`` means correlation-vars only)."""
    return _STATE["provider"]


def enabled() -> bool:
    return _STATE["initialized"] and _STATE["tracer"] is not None


def init_observability_profiling(**bridge) -> None:
    """No-op alias kept for API stability across profiles."""
    return


def _bridge_ids_to_real_trace() -> None:
    """Expose correlation trace ids via OTel-ish non-invasive names."""
    from . import context as ctx

    if not ctx.trace_id_var.get():
        return
    # No SDK: just remember ids so logs can co-reference them.
    _STATE["provider"] = "correlation"


def init_telemetry() -> bool:
    """Try to initialize the OTel SDK + OTLP/HTTP exporter if installed.

    Returns True when a provider is actually active. Safe to call repeatedly;
    falls back gracefully in every failure mode.
    """
    if _STATE["initialized"]:
        return enabled()
    _STATE["initialized"] = True
    endpoint = os.getenv("NOYA_OTEL_EXPORTER_OTLP_HTTP_ENDPOINT", "").strip()
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (  # noqa: F401
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )

        resource = Resource.create(
            {
                "service.name": os.getenv("NOYA_SERVICE_NAME", "noyabot"),
                "deployment.environment": os.getenv("NOYA_ENVIRONMENT", "development"),
                "service.version": os.getenv("NOYA_RELEASE", ""),
                "process.runtime.name": "python",
            }
        )
        provider = TracerProvider(resource=resource)
        exporter = None
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(endpoint=endpoint, timeout=5)
            except Exception:
                logger.warning("otel_otlp_exporter_unavailable", exc_info=True)
        if exporter is not None:
            provider.add_span_processor(_SafeBatchProcessor(exporter, console=False))
            _STATE["provider"] = "otlp/http"
        elif os.getenv("NOYA_OTEL_CONSOLE", "false").lower() in {"1", "true", "yes", "on"}:
            provider.add_span_processor(_SafeBatchProcessor(ConsoleSpanExporter(), console=True))
            _STATE["provider"] = "console"
        else:
            # SDK spans stay in-process only until an exporter is configured.
            _STATE["provider"] = "sdk"
        otel_trace.set_tracer_provider(provider)
        _STATE["tracer"] = otel_trace.get_tracer("botapp.noya")
        try:
            from . import otel_signals
            otel_signals.configure()
        except Exception:
            pass
        return True
    except Exception:
        _STATE["provider"] = "correlation"
        _STATE["tracer"] = None
        _bridge_ids_to_real_trace()
        return False


class _SafeBatchProcessor:
    """Wraps a span processor so export failures never raise into app code."""

    def __init__(self, exporter, *, console: bool):
        try:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            self._inner = BatchSpanProcessor(exporter)
        except Exception:
            self._inner = None

    @property
    def inner(self):
        return self._inner

    def on_start(self, span, parent_context):
        if self._inner:
            self._inner.on_start(span, parent_context)

    def on_end(self, span):
        if self._inner:
            try:
                self._inner.on_end(span)
            except Exception:
                pass

    def shutdown(self):
        if self._inner:
            try:
                self._inner.shutdown()
            except Exception:
                pass


def start_span(name: str, **attributes):
    """Context manager. Falls back to correlation-id context vars."""
    try:
        if enabled() and _STATE["tracer"]:
            import contextlib

            from opentelemetry.trace import SpanKind

            @contextlib.contextmanager
            def _otel():
                with _STATE["tracer"].start_as_current_span(
                    name, kind=SpanKind.INTERNAL
                ) as span:
                    for key, value in attributes.items():
                        try:
                            span.set_attribute(str(key), str(value)[:200])
                        except Exception:
                            pass
                    yield span

            return _otel()
    except Exception:
        pass

    import contextlib
    from . import context as ctx

    @contextlib.contextmanager
    def _fallback():
        ctx.span_id_var.set(ctx.otel_safe_span_id(name))
        ctx.operation_var.set(name)
        yield None

    return _fallback()


def shutdown_for_tests() -> None:
    _STATE["initialized"] = False
    _STATE["tracer"] = None
    _STATE["provider"] = "none"
