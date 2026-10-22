"""Authenticated, bounded diagnostics endpoints for operators.

Only read-only process snapshots and pre-registered checks are exposed.  No
request value is ever executed as code or passed to a shell.
"""
from __future__ import annotations

import json
import threading
import time
import os
import uuid
from collections import deque

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from botapp.api import api_authenticated
from botapp.observability import context, health, logging as obs_logging, metrics, redaction, tracing
from botapp.observability.instrumentation import ops_auth_failure, ops_diagnostic_run, ops_rate_limited
from botapp.channel_insights import configuration_status as channel_configuration_status

_RATE_LOCK = threading.Lock()
_RATE: dict[str, deque[float]] = {}
_RATE_WINDOW = 60.0
_RATE_LIMIT = 30
_RUNS_LOCK = threading.Lock()
_RUNS: dict[str, dict] = {}
_MAX_RUNS = 100


def _actor_key(request) -> str:
    actor = getattr(request, "api_actor", None)
    return f"{type(actor).__name__}:{getattr(actor, 'id', None) or getattr(actor, 'pk', None) or request.META.get('REMOTE_ADDR', '')}"


def _allowed(request, scope: str) -> bool:
    now = time.monotonic()
    key = f"{scope}:{_actor_key(request)}"
    with _RATE_LOCK:
        bucket = _RATE.setdefault(key, deque())
        while bucket and now - bucket[0] > _RATE_WINDOW:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT:
            ops_rate_limited(scope)
            return False
        bucket.append(now)
    return True


def _guard(request, scope: str):
    if not _allowed(request, scope):
        return JsonResponse({"error": "rate limit exceeded"}, status=429)
    return None


_RAW_TEXT_KEYS = {
    "prompt", "raw_prompt", "user_prompt", "input_text", "message_text", "caption", "query",
    "message", "body", "text", "content", "reasoning", "thinking",
}


def _ops_redact(value):
    """Redact credentials and user supplied prompt-like fields from ops output."""
    clean = redaction.redact_value(value)
    if isinstance(clean, dict):
        return {key: "[REDACTED]" if str(key).casefold() in _RAW_TEXT_KEYS else _ops_redact(item) for key, item in clean.items()}
    if isinstance(clean, list):
        return [_ops_redact(item) for item in clean]
    return clean


def _json(request):
    try:
        value = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _auth(view):
    return api_authenticated(view)


@require_http_methods(["GET"])
@_auth
def ops_live(request):
    if (blocked := _guard(request, "live")):
        return blocked
    return JsonResponse({"status": "ok", "pid": os.getpid()})


@require_http_methods(["GET"])
@_auth
def ops_ready(request):
    if (blocked := _guard(request, "ready")):
        return blocked
    ready, error = health.db_ready()
    return JsonResponse({"status": "ok" if ready else "degraded", "database": ready,
                         **({"error_type": error} if error else {})}, status=200 if ready else 503)


@require_http_methods(["GET"])
@_auth
def ops_dependencies(request):
    if (blocked := _guard(request, "dependencies")):
        return blocked
    ready, error = health.db_ready()
    return JsonResponse({"database": {"ok": ready, **({"error_type": error} if error else {})},
                         "channel_analysis": channel_configuration_status(),
                         "telemetry": {"provider": tracing.trace_provider(), "enabled": tracing.enabled()}})


@require_http_methods(["GET"])
@_auth
def ops_health(request):
    if (blocked := _guard(request, "health")):
        return blocked
    ready, error = health.db_ready()
    payload = {
        "status": "ok" if ready else "degraded",
        "checks": {"database": {"ok": ready}, "channel_analysis": channel_configuration_status()},
    }
    if error:
        payload["checks"]["database"]["error_type"] = str(error)[:80]
    return JsonResponse(payload)


@require_http_methods(["GET"])
@_auth
def ops_runtime(request):
    if (blocked := _guard(request, "runtime")):
        return blocked
    return JsonResponse({
        "service": os.getenv("NOYA_SERVICE_NAME", "noyabot"),
        "environment": os.getenv("NOYA_ENVIRONMENT", "development"),
        "release": os.getenv("NOYA_RELEASE", ""),
        "git_commit": obs_logging.resolve_git_commit(),
        "uptime_seconds": round(health.process_uptime_seconds(), 2),
        "facts": _ops_redact(health.system_facts()),
        "trace_provider": tracing.trace_provider(),
        "capabilities": ["health", "runtime", "logs", "metrics", "traces", "diagnostics"],
    })


@require_http_methods(["GET"])
@_auth
def ops_logs(request):
    if (blocked := _guard(request, "logs")):
        return blocked
    try:
        limit = max(1, min(int(request.GET.get("limit", 100)), 200))
        cursor = max(0, int(request.GET.get("cursor", 0)))
    except (TypeError, ValueError):
        return JsonResponse({"error": "limit and cursor must be integers"}, status=400)
    entries, newest = obs_logging.RING.since(cursor)
    level = str(request.GET.get("level", "")).casefold()
    trace_id = str(request.GET.get("trace_id", ""))
    correlation_id = str(request.GET.get("correlation_id", ""))
    service = str(request.GET.get("service", ""))
    entries = [item for item in entries if (not level or str(item.get("level", "")).casefold() == level)
               and (not trace_id or str(item.get("trace_id", "")) == trace_id)
               and (not correlation_id or str(item.get("correlation_id", "")) == correlation_id)
               and (not service or str(item.get("service", "")) == service)]
    entries = [_ops_redact(item) for item in (entries[-limit:] if cursor == 0 else entries[:limit])]
    return JsonResponse({"entries": entries, "next_cursor": newest})


@require_http_methods(["GET"])
@_auth
def ops_metrics(request):
    if (blocked := _guard(request, "metrics")):
        return blocked
    return JsonResponse({"metrics": _ops_redact(metrics.snapshot())})


@require_http_methods(["GET"])
@_auth
def ops_traces(request):
    if (blocked := _guard(request, "traces")):
        return blocked
    return JsonResponse({"provider": tracing.trace_provider(), "enabled": tracing.enabled(), "context": _ops_redact(context.snapshot())})


@require_http_methods(["GET"])
@_auth
def ops_trace_detail(request, trace_id):
    if (blocked := _guard(request, "trace")):
        return blocked
    wanted = str(trace_id)[:64]
    entries = [
        _ops_redact(item)
        for item in obs_logging.RING.tail(500)
        if str(item.get("trace_id", "")) == wanted
    ]
    return JsonResponse({"trace_id": wanted, "entries": entries, "count": len(entries)})


@require_http_methods(["GET", "POST"])
@_auth
def ops_diagnostics(request):
    if (blocked := _guard(request, "diagnostics")):
        return blocked
    if request.method == "GET":
        return JsonResponse({
            "health": {"database": health.db_ready()[0]},
            "runtime": {"uptime_seconds": round(health.process_uptime_seconds(), 2)},
            "metrics": metrics.snapshot(),
            "traces": {"provider": tracing.trace_provider(), "enabled": tracing.enabled()},
        })
    body = _json(request)
    test_id = str((body or {}).get("test_id", ""))
    check = SAFE_TESTS.get(test_id)
    if check is None:
        ops_auth_failure("unknown_diagnostic")
        return JsonResponse({"error": "unknown test_id", "tests": sorted(SAFE_TESTS)}, status=400)
    run_id = uuid.uuid4().hex[:16]
    try:
        result = check()
        outcome = "ok" if result.get("ok") else "failed"
    except Exception as exc:  # bounded checks must never take down request
        result, outcome = {"ok": False, "error_type": type(exc).__name__}, "failed"
    ops_diagnostic_run(test_id=test_id, group="ops", outcome=outcome)
    record = {"run_id": run_id, "test_id": test_id, "status": "completed", "outcome": outcome,
              "result": _ops_redact(result), "started_at": time.time(), "completed_at": time.time()}
    with _RUNS_LOCK:
        _RUNS[run_id] = record
        while len(_RUNS) > _MAX_RUNS:
            _RUNS.pop(next(iter(_RUNS)))
    return JsonResponse({"test_id": test_id, "run_id": run_id, "status": "completed", "outcome": outcome, "result": _ops_redact(result)})


@require_http_methods(["GET"])
@_auth
def ops_diagnostic_run_detail(request, run_id):
    if (blocked := _guard(request, "diagnostic_result")):
        return blocked
    with _RUNS_LOCK:
        record = _RUNS.get(str(run_id))
    if record is None:
        return JsonResponse({"error": "run not found"}, status=404)
    return JsonResponse(_ops_redact(record))


def _db_check():
    ok, error = health.db_ready()
    return {"ok": ok, **({"error_type": error} if error else {})}


def _process_check():
    return {"ok": True, "uptime_seconds": round(health.process_uptime_seconds(), 2)}


SAFE_TESTS = {"database": _db_check, "process": _process_check}


@require_http_methods(["GET"])
@_auth
def ops_tests(request):
    if (blocked := _guard(request, "tests")):
        return blocked
    return JsonResponse({"tests": [{"id": key, "safe": True} for key in sorted(SAFE_TESTS)]})
