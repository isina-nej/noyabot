"""Authenticated, bounded, private remote observability endpoints.

Every endpoint requires an operator-only read/run token or a CSRF-checked
superuser session. Runs are enqueued to an isolated worker, never executed in
the HTTP process; results/logs persist in separate bounded ops storage.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque, defaultdict

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from botapp.observability import context, health, logging as obs_logging, metrics, redaction, tracing
from botapp.observability import diagnostics, store
from botapp.observability.instrumentation import ops_auth_failure, ops_rate_limited
from botapp.observability.ops_auth import ops_authenticated

_RATE_LOCK = threading.Lock()
_RATE = defaultdict(deque)
_TRACE_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _limit(request, key: str, *, max_calls=60):
    # Limit by token-derived pseudonym (never store token or expose it).
    raw = request.headers.get("X-Noya-Ops-Key", "") or request.headers.get("X-API-Key", "")
    actor = hashlib.sha256(raw.encode()).hexdigest()[:12] if raw else str(request.ops_actor)
    now = time.monotonic()
    label = (key, actor)
    with _RATE_LOCK:
        bucket = _RATE[label]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= max_calls:
            ops_rate_limited(key)
            return JsonResponse({"error": "rate limit exceeded"}, status=429)
        bucket.append(now)
    return None


def _response(payload, *, status=200):
    return JsonResponse(redaction.redact_value(payload), status=status)


def _json(request):
    if len(request.body) > 4096:
        return None
    try:
        value = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@require_http_methods(["GET"])
@ops_authenticated
def ops_live(request):
    if blocked := _limit(request, "live"):
        return blocked
    return _response({"status": "ok"})


@require_http_methods(["GET"])
@ops_authenticated
def ops_ready(request):
    if blocked := _limit(request, "ready"):
        return blocked
    ok, error = health.db_ready()
    return _response({"status": "ok" if ok else "degraded",
                      "database": ok, "error_type": error or None}, status=200 if ok else 503)


@require_http_methods(["GET"])
@ops_authenticated
def ops_health(request):
    if blocked := _limit(request, "health"):
        return blocked
    ok, error = health.db_ready()
    return _response({"status": "ok" if ok else "degraded", "checks": {
        "database": {"ok": ok, "error_type": error or None},
        "bot_worker": store.read_heartbeat(),
        "diagnostics_worker": store.read_heartbeat("diagnostics-worker"),
    }})


@require_http_methods(["GET"])
@ops_authenticated
def ops_dependencies(request):
    if blocked := _limit(request, "dependencies"):
        return blocked
    ok, error = health.db_ready()
    return _response({
        "database": {"ok": ok, "error_type": error or None},
        "telemetry": {"provider": tracing.trace_provider(), "enabled": tracing.enabled()},
        "bot_worker": store.read_heartbeat(),
    })


@require_http_methods(["GET"])
@ops_authenticated
def ops_runtime(request):
    if blocked := _limit(request, "runtime"):
        return blocked
    return _response({
        "service": os.getenv("NOYA_SERVICE_NAME", "noyabot"),
        "environment": os.getenv("NOYA_ENVIRONMENT", "development"),
        "release": os.getenv("NOYA_RELEASE", "")[:80],
        "git_commit": obs_logging.resolve_git_commit(),
        "uptime_seconds": round(health.process_uptime_seconds(), 2),
        "facts": health.system_facts(),
        "bot_worker": store.read_heartbeat(),
        "trace_provider": tracing.trace_provider(),
        "capabilities": ["health", "runtime", "logs", "metrics", "traces", "diagnostics"],
    })


@require_http_methods(["GET"])
@ops_authenticated
def ops_logs(request):
    if blocked := _limit(request, "logs"):
        return blocked
    try:
        limit = max(1, min(int(request.GET.get("limit", "100")), 200))
        cursor = max(0, int(request.GET.get("cursor", "0")))
        since_raw, until_raw = request.GET.get("since"), request.GET.get("until")
        since = float(since_raw) if since_raw else None
        until = float(until_raw) if until_raw else None
    except (TypeError, ValueError):
        return _response({"error": "invalid pagination/time parameters"}, status=400)
    entries, next_cursor = store.query_events(
        cursor=cursor, limit=limit,
        level=request.GET.get("level", ""), service=request.GET.get("service", ""),
        trace_id=request.GET.get("trace_id", ""),
        correlation_id=request.GET.get("correlation_id", ""),
        since=since, until=until,
    )
    return _response({"entries": entries, "next_cursor": next_cursor,
                      "storage": "shared_bounded_sqlite"})


@require_http_methods(["GET"])
@ops_authenticated
def ops_metrics(request):
    if blocked := _limit(request, "metrics"):
        return blocked
    return _response({"metrics": metrics.snapshot(), "scope": "http_process",
                      "note": "Use OTLP backend for cross-process/global metrics."})


@require_http_methods(["GET"])
@ops_authenticated
def ops_traces(request):
    if blocked := _limit(request, "traces"):
        return blocked
    return _response({"provider": tracing.trace_provider(), "enabled": tracing.enabled(),
                      "context": context.snapshot()})


@require_http_methods(["GET"])
@ops_authenticated
def ops_trace_detail(request, trace_id):
    if blocked := _limit(request, "trace"):
        return blocked
    if not _TRACE_RE.fullmatch(trace_id):
        return _response({"error": "invalid trace id"}, status=400)
    entries, _ = store.query_events(trace_id=trace_id, limit=200)
    return _response({"trace_id": trace_id, "entries": entries, "count": len(entries)})


@require_http_methods(["GET"])
@ops_authenticated
def ops_tests(request):
    if blocked := _limit(request, "tests"):
        return blocked
    return _response({"tests": diagnostics.test_catalog()})


@require_http_methods(["GET", "POST"])
@ops_authenticated
def ops_diagnostics(request):
    if blocked := _limit(request, "diagnostics", max_calls=6 if request.method == "POST" else 60):
        return blocked
    if request.method == "GET":
        return _response({"tests": diagnostics.test_catalog(),
                          "bot_worker": store.read_heartbeat(),
                          "metrics": metrics.snapshot()})
    payload = _json(request)
    if not payload or set(payload) != {"test_id"}:
        return _response({"error": "JSON body must contain only test_id"}, status=400)
    test_id = payload.get("test_id")
    if not isinstance(test_id, str) or len(test_id) > 64:
        return _response({"error": "invalid test_id"}, status=400)
    enabled, why = diagnostics.can_enqueue(test_id)
    if not enabled:
        ops_auth_failure("unknown_or_disabled_diagnostic")
        return _response({"error": why}, status=403 if why == "staging diagnostics disabled" else 400)
    actor = str(getattr(request, "ops_actor", "unknown"))[:64]
    created = store.create_run(test_id, actor)
    return _response(created, status=202)


@require_http_methods(["GET"])
@ops_authenticated
def ops_diagnostic_run_detail(request, run_id):
    if blocked := _limit(request, "diagnostic_result"):
        return blocked
    if not _RUN_ID_RE.fullmatch(run_id):
        return _response({"error": "invalid run id"}, status=400)
    record = store.get_run(run_id)
    return _response(record) if record else _response({"error": "run not found"}, status=404)


@require_http_methods(["GET"])
@ops_authenticated
def ops_diagnostic_run_events(request, run_id):
    if blocked := _limit(request, "diagnostic_events"):
        return blocked
    if not _RUN_ID_RE.fullmatch(run_id):
        return _response({"error": "invalid run id"}, status=400)
    events = store.run_events(run_id)
    return _response({"run_id": run_id, "events": events}) if events is not None else _response({"error": "run not found"}, status=404)


@require_http_methods(["GET"])
@ops_authenticated
def ops_diagnostic_run_report(request, run_id):
    if blocked := _limit(request, "diagnostic_report"):
        return blocked
    if not _RUN_ID_RE.fullmatch(run_id):
        return _response({"error": "invalid run id"}, status=400)
    record = store.get_run(run_id)
    if not record:
        return _response({"error": "run not found"}, status=404)
    return _response({"run_id": run_id, "test_id": record["test_id"],
                      "status": record["status"], "outcome": record["outcome"],
                      "result": record["result"], "error_type": record["error_type"]})
