"""Real-pipeline instrumentation — thin spans/metrics on actual code paths.

Only stages that really exist in the bot are instrumented; no fabricated
events.  Telemetry failures NEVER raise into the bot (best-effort, wrapped).
"""
from __future__ import annotations

import time
from functools import wraps
from contextlib import contextmanager

from . import context as ctx
from . import metrics as m
from . import tracing as tr


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return


@contextmanager
def stage(name: str, **attributes):
    """One pipeline stage: metrics histogram + span (real or fallback)."""
    start = time.perf_counter()
    span = tr.start_span(name, **attributes)
    try:
        with span:
            try:
                yield
            except Exception as exc:
                _safe(m.inc, m.TELEGRAM_HANDLER_FAILURES, stage=name.split(".")[0])
                raise
    finally:
        _safe(m.observe, _duration_of(name), (time.perf_counter() - start) * 1000.0,
              **_low_card(name))


def instrument_async(name: str, **attributes):
    """Decorate a real async stage without changing its return contract."""
    def decorator(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            with stage(name, **attributes):
                return await fn(*args, **kwargs)
        return wrapped
    return decorator


def _duration_of(name: str) -> str:
    base = name.split(".")[0]
    return {
        "telegram": m.TELEGRAM_UPDATE_LATENCY,
        "ai": m.AI_LATENCY,
        "agent": m.AGENT_TOOL_LATENCY,
        "harness": m.HARNESS_STEP_DURATION,
        "memory": m.MEMORY_LATENCY,
    }.get(base, "noya.misc.stage_latency_ms")


def _low_card(name: str):
    base = name.split(".")[0]
    if base in {"telegram", "ai", "agent", "harness", "memory", "emotion"}:
        return {f"{base.replace('_', '')}_stage_group": base}
    return {}


# ── concrete stage helpers (one line each, called from real pipelines) ─────

def telegram_update_received(update_id: int, chat_id=None, user_id=None, operation=""):
    _safe(ctx.bind, update_id=int(update_id), chat_id=chat_id, user_id=user_id,
          operation=operation)
    _safe(m.inc, m.TELEGRAM_UPDATES_RECEIVED)


def telegram_update_ignored(reason: str):
    _safe(m.inc, m.TELEGRAM_UPDATES_IGNORED, reason=_reason(reason))


def telegram_update_processed(update_id: int):
    _safe(m.inc, m.TELEGRAM_UPDATES_PROCESSED)


def telegram_send_failure(kind: str):
    _safe(m.inc, m.TELEGRAM_SEND_FAILURES, kind=_reason(kind))


def security_permission_check(*, allowed: bool, permission: str = ""):
    _safe(m.inc, "noya.security.permission_checks",
          allowed="true" if allowed else "false",
          permission=_label(permission))


def relationship_state_load(*, status: str = "", persisted: bool = False):
    _safe(m.inc, "noya.relationship.state_loads",
          status=_label(status) or "unknown", source="db" if persisted else "none")


def relationship_state_transition(*, from_status: str, to_status: str):
    _safe(m.inc, m.EMOTION_CONFLICT_TRANSITIONS,
          **{"from": _label(from_status), "to": _label(to_status)})


def emotion_event_classified(*, kind: str, confidence: float, playful: bool = False):
    _safe(m.inc, m.EMOTION_CLASSIFICATIONS, kind=_label(kind))
    if kind in ("sincere_apology", "acknowledgement_of_harm", "behavioral_improvement", "reconciliation"):
        _safe(m.set_gauge, "noya.emotion.last_repair_confidence",
              round(float(confidence), 2), kind=_label(kind))


def emotion_duplicate_apology():
    _safe(m.inc, m.EMOTION_DUPLICATE_EVENTS)


def emotion_persistence_failure(kind: str):
    _safe(m.inc, m.EMOTION_PERSISTENCE_FAILURES, kind=_reason(kind))


def ai_provider_request_begin(model: str = "") -> None:
    _safe(m.inc, m.AI_PROVIDER_REQUESTS, model=_label(model))


def ai_provider_request_end(*, duration_ms: float, status: str, model: str = "",
                           http_status: int = 0, timed_out: bool = False,
                           retry: int = 0, tokens_in: float | None = None,
                           tokens_out: float | None = None, streaming_ms: float | None = None):
    ok = status == "ok"
    _safe(m.observe, m.AI_LATENCY, duration_ms, model=_label(model), status=_reason(status))
    if not ok:
        _safe(m.inc, m.AI_PROVIDER_FAILURES, model=_label(model), kind=_reason(status))
    if status in {"malformed", "invalid_response"}:
        _safe(m.inc, m.AI_PROVIDER_MALFORMED, model=_label(model))
    if timed_out:
        _safe(m.inc, m.AI_PROVIDER_TIMEOUTS, model=_label(model))
    if retry:
        _safe(m.inc, "noya.ai.retries", max(1, int(retry)), model=_label(model))
    if streaming_ms:
        _safe(m.observe, m.AI_STREAM_TIME, streaming_ms, model=_label(model))
    if tokens_in:
        _safe(m.inc, m.AI_TOKENS_INPUT, tokens_in, model=_label(model))
    if tokens_out:
        _safe(m.inc, m.AI_TOKENS_OUTPUT, tokens_out, model=_label(model))


def ai_sse_malformed(endpoint: str = ""):
    _safe(m.inc, m.AI_SSE_ERRORS, kind="malformed")
    _safe(m.inc, m.AI_PROVIDER_MALFORMED, endpoint=_label(endpoint) or "_internal")
    _safe(m.emit, "ai.sse_malformed", level="error", endpoint=_label(endpoint) or "_internal")


def ai_response_extracted(*, content_chars: int, reasoning_chars: int = 0):
    _safe(m.set_gauge, "noya.ai.last_response_chars", round(float(content_chars)))


def agent_tool_dispatch(*, tool: str, duration_ms: float, status: str):
    ok = status == "ok"
    _safe(m.inc, m.AGENT_TOOL_CALLS, tool=_label(tool), status=_reason(status))
    if not ok:
        _safe(m.inc, m.AGENT_TOOL_FAILURES, tool=_label(tool), status=_reason(status))
    _safe(m.observe, m.AGENT_TOOL_LATENCY, duration_ms, tool=_label(tool))


def harness_run_started(run_id: str = ""):
    _safe(m.inc, m.HARNESS_RUNS_STARTED)


def harness_step(*, step_id: str, tool_name: str, outcome: str, duration_ms: float,
                 error_class: str = ""):
    if outcome == "ok":
        _safe(m.inc, m.HARNESS_STEPS, tool=_label(tool_name))
    elif outcome in ("failed", "denied"):
        _safe(m.inc, m.HARNESS_STEP_FAILURES, tool=_label(tool_name),
              kind=_reason(error_class or outcome))
    _safe(m.observe, m.HARNESS_STEP_DURATION, duration_ms, tool=_label(tool_name))


def memory_lookup_failure(*, kind: str, direction: str = "read"):
    _safe(m.inc, m.MEMORY_READ_FAILURES if direction == "read" else m.MEMORY_WRITE_FAILURES,
          kind=_reason(kind))


def output_sanitized(*, dropped_lines: int = 0, empty: bool = False):
    _safe(m.inc, m.OUTPUT_SANITIZATIONS, outcome="empty" if empty else "cleaned")
    if empty:
        _safe(m.inc, m.OUTPUT_EMPTY_RESPONSE)


def internal_artifact_detected(*, component: str, kind: str = "annotation"):
    """A provider scratch-note leaked into the response path."""
    _safe(m.inc, m.OUTPUT_ARTIFACTS_DETECTED, component=_label(component), kind=_reason(kind))
    _safe(m.emit, "output.internal_artifact_detected", level="warning",
          component=_label(component), artifact_kind=_reason(kind))


def ops_auth_failure(reason: str = "unauthorized"):
    _safe(m.inc, m.OPS_AUTH_FAILURES, reason=_reason(reason))


def ops_rate_limited(scope: str):
    _safe(m.inc, m.OPS_RATE_LIMITED, scope=_label(scope))


def ops_diagnostic_run(*, test_id: str, group: str, outcome: str):
    _safe(m.inc, m.OPS_DIAGNOSTIC_RUNS, group=_label(group), outcome=_reason(outcome))


def uptime_gauge():
    from .health import process_uptime_seconds, rss_mb

    _safe(m.set_gauge, m.UPS_SECONDS, process_uptime_seconds())
    rss = rss_mb()
    if rss:
        _safe(m.set_gauge, m.PROCESS_RSS_MB, rss)


def _reason(value: str) -> str:
    value = str(value or "").strip()
    return (value[:40] or "unknown").replace(" ", "_").replace(":", "_")


def _label(value: str) -> str:
    value = str(value or "").strip().lower()
    return (value[:48] or "unknown").replace(" ", "_")
