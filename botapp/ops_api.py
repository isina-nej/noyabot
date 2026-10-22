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
from datetime import datetime
from functools import wraps
from collections import deque

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from botapp.api import api_authenticated
from botapp.observability import context, health, logging as obs_logging, metrics, redaction, tracing
from botapp.observability.instrumentation import ops_auth_failure, ops_diagnostic_run, ops_rate_limited
from botapp.channel_insights import configuration_status as channel_configuration_status
from botapp.models import StaffAPIKey

_RATE_LOCK = threading.Lock()
_RATE: dict[str, deque[float]] = {}
_RATE_WINDOW = 60.0
_RATE_LIMIT = 30
_MAX_RATE_ACTORS = 2048
_AUTH_RATE = {}
_AUTH_RATE_LIMIT = 10
_AUTH_RATE_WINDOW = 60.0
_RUNS_LOCK = threading.Lock()
_RUNS: dict[str, dict] = {}
_MAX_RUNS = 100
_RUN_SLOTS = threading.BoundedSemaphore(2)
try:
    _DIAGNOSTIC_TIMEOUT_SECONDS = max(
        0.1, min(float(os.getenv("NOYA_DIAGNOSTIC_TIMEOUT_SECONDS", "5")), 30.0)
    )
except (TypeError, ValueError):
    _DIAGNOSTIC_TIMEOUT_SECONDS = 5.0
_MAX_RESULT_BYTES = 32_000
_MAX_RESPONSE_BYTES = 256_000
_MAX_EVENTS = 50


def _actor_key(request) -> str:
    actor = getattr(request, "api_actor", None)
    actor_ref = (
        getattr(actor, "prefix", None)
        or getattr(actor, "id", None)
        or getattr(actor, "pk", None)
        or request.META.get("REMOTE_ADDR", "")
    )
    return f"{type(actor).__name__}:{actor_ref}"


def _allowed(request, scope: str) -> bool:
    now = time.monotonic()
    key = f"{scope}:{_actor_key(request)}"
    with _RATE_LOCK:
        if key not in _RATE and len(_RATE) >= _MAX_RATE_ACTORS:
            _RATE.pop(next(iter(_RATE)), None)
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


def _auth_actor_key(request) -> str:
    """Use the peer address for failed-auth throttling; never key on secrets."""
    return str(request.META.get("REMOTE_ADDR", ""))[:64] or "unknown"


def _auth_failure_allowed(request) -> bool:
    now = time.monotonic()
    key = _auth_actor_key(request)
    with _RATE_LOCK:
        if key not in _AUTH_RATE and len(_AUTH_RATE) >= _MAX_RATE_ACTORS:
            _AUTH_RATE.pop(next(iter(_AUTH_RATE)), None)
        bucket = _AUTH_RATE.setdefault(key, deque())
        while bucket and now - bucket[0] > _AUTH_RATE_WINDOW:
            bucket.popleft()
        if len(bucket) >= _AUTH_RATE_LIMIT:
            return False
        bucket.append(now)
    return True


_RAW_TEXT_KEYS = {
    "prompt", "raw_prompt", "user_prompt", "input_text", "message_text", "caption", "query",
    "message", "body", "text", "content", "reasoning", "thinking",
    "chat_id", "telegram_chat_id", "group_chat_id", "user_id", "target_user_id",
    "actor_user_id", "message_id", "telegram_update_id",
}


def _ops_redact(value):
    """Redact credentials and user supplied prompt-like fields from ops output."""
    clean = redaction.redact_value(value)
    if isinstance(clean, dict):
        return {
            key: "[REDACTED]" if str(key).casefold() in _RAW_TEXT_KEYS else _ops_redact(item)
            for key, item in clean.items()
        }
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
    authenticated = api_authenticated(view)

    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        response = authenticated(request, *args, **kwargs)
        if getattr(response, "status_code", 200) == 401:
            ops_auth_failure("unauthorized")
            if not _auth_failure_allowed(request):
                ops_rate_limited("auth")
                return JsonResponse({"error": "rate limit exceeded"}, status=429)
        return response

    return wrapped


def _actor_allows_chat(request, chat_id: int) -> bool:
    actor = getattr(request, "api_actor", None)
    return not isinstance(actor, StaffAPIKey) or actor.allows_chat(chat_id)


def _entry_chat_id(entry: dict) -> int | None:
    for key in ("chat_id", "telegram_chat_id", "group_chat_id"):
        if key not in entry:
            continue
        try:
            return int(entry[key])
        except (TypeError, ValueError):
            return None
    return None


def _entry_visible(request, entry: dict, requested_chat_id: int | None = None) -> bool:
    chat_id = _entry_chat_id(entry)
    if requested_chat_id is not None and chat_id != requested_chat_id:
        return False
    if chat_id is None and any(key in entry for key in ("chat_id", "telegram_chat_id", "group_chat_id")):
        # A scoped key must not receive malformed or unparseable chat metadata.
        actor = getattr(request, "api_actor", None)
        if isinstance(actor, StaffAPIKey) and actor.allowed_chat_ids:
            return False
    return chat_id is None or _actor_allows_chat(request, chat_id)


def _timestamp_seconds(value) -> float | None:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None


def _bounded_result(value):
    clean = _ops_redact(value)
    try:
        encoded = json.dumps(clean, ensure_ascii=False, default=str).encode("utf-8")
    except Exception:
        return {"ok": False, "error_type": "ResultSerializationError"}
    if len(encoded) <= _MAX_RESULT_BYTES:
        return clean
    return {"ok": False, "truncated": True, "error_type": "ResultTooLarge"}


def _bounded_entries(entries: list[dict]) -> list[dict]:
    bounded = []
    for entry in entries:
        candidate = bounded + [entry]
        try:
            if len(json.dumps(candidate, ensure_ascii=False, default=str).encode("utf-8")) > _MAX_RESPONSE_BYTES:
                break
        except Exception:
            break
        bounded.append(entry)
    return bounded


def _append_run_event(record: dict, event: str, status: str, **fields) -> None:
    events = record.setdefault("events", [])
    event_record = {"timestamp": time.time(), "event": event, "status": status}
    event_record.update({key: value for key, value in fields.items() if key in {"error_type", "duration_ms"}})
    events.append(event_record)
    del events[:-_MAX_EVENTS]


def _execute_check(check):
    """Run one allowlisted check in a daemon thread with a hard request bound."""
    if not _RUN_SLOTS.acquire(blocking=False):
        return {"ok": False, "error_type": "DiagnosticConcurrencyLimit"}, False, True
    holder = {}

    def worker():
        try:
            result = check()
            holder["result"] = result if isinstance(result, dict) else {"ok": False, "error_type": "InvalidResult"}
        except Exception as exc:  # bounded checks must never take down request
            holder["result"] = {"ok": False, "error_type": type(exc).__name__}
        finally:
            _RUN_SLOTS.release()

    thread = threading.Thread(target=worker, name="noya-diagnostic", daemon=True)
    thread.start()
    thread.join(_DIAGNOSTIC_TIMEOUT_SECONDS)
    if thread.is_alive():
        return {"ok": False, "error_type": "TimeoutError", "timed_out": True}, True, False
    return holder.get("result", {"ok": False, "error_type": "NoResult"}), False, False


def _feature_flags() -> dict[str, bool]:
    """Expose only a fixed, non-secret subset of deployment toggles."""
    names = {
        "message_archive": "MESSAGE_ARCHIVE_ENABLED",
        "search": "NOYA_SEARCH_ENABLED",
        "channel_analysis": "CHANNEL_ANALYSIS_ENABLED",
        "agent_harness": "AGENT_HARNESS_ENABLED",
        "agent_harness_ai": "AGENT_HARNESS_AI_ENABLED",
    }
    defaults = {
        "message_archive": "false",
        "search": "true",
        "channel_analysis": "true",
        "agent_harness": "true",
        "agent_harness_ai": "false",
    }
    truthy = {"1", "true", "yes", "on"}
    flags = {key: os.getenv(env, defaults[key]).casefold() in truthy for key, env in names.items()}
    flags["otel"] = bool(os.getenv("NOYA_OTEL_EXPORTER_OTLP_HTTP_ENDPOINT", "").strip()) or (
        os.getenv("NOYA_OTEL_CONSOLE", "false").casefold() in truthy
    )
    return flags


@require_http_methods(["GET"])
@_auth
def ops_live(request):
    if (blocked := _guard(request, "live")):
        return blocked
    return JsonResponse({"status": "ok", "pid": os.getpid(),
                         "uptime_seconds": round(health.process_uptime_seconds(), 2)})


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
    return JsonResponse({"process": {"ok": True, "pid": os.getpid()},
                         "database": {"ok": ready, **({"error_type": error} if error else {})},
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
        "checks": {"process": {"ok": True}, "database": {"ok": ready},
                    "channel_analysis": channel_configuration_status()},
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
        "worker": {"pid": os.getpid(), "alive": True},
        "feature_flags": _feature_flags(),
        "trace_provider": tracing.trace_provider(),
        "capabilities": [
            "health", "runtime", "logs", "metrics", "traces", "diagnostics",
            "diagnostic_tests", "diagnostic_runs", "diagnostic_events", "diagnostic_reports",
        ],
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
    level = str(request.GET.get("level", ""))[:20].casefold()
    trace_id = str(request.GET.get("trace_id", ""))[:128]
    correlation_id = str(request.GET.get("correlation_id", ""))[:128]
    request_id = str(request.GET.get("request_id", ""))[:128]
    service = str(request.GET.get("service", ""))[:80]
    raw_since = request.GET.get("since", request.GET.get("start"))
    raw_until = request.GET.get("until", request.GET.get("end"))
    try:
        since = _timestamp_seconds(raw_since) if raw_since not in (None, "") else None
        until = _timestamp_seconds(raw_until) if raw_until not in (None, "") else None
    except (TypeError, ValueError):
        since = until = None
    if ((raw_since not in (None, "") and since is None)
            or (raw_until not in (None, "") and until is None)):
        return JsonResponse({"error": "since and until must be timestamps"}, status=400)
    raw_chat_id = request.GET.get("chat_id")
    if raw_chat_id in (None, ""):
        requested_chat_id = None
    else:
        try:
            requested_chat_id = int(raw_chat_id)
        except (TypeError, ValueError):
            return JsonResponse({"error": "chat_id must be an integer"}, status=400)
        if not _actor_allows_chat(request, requested_chat_id):
            return JsonResponse({"error": "chat_id not allowed for this API key"}, status=403)
    entries = [item for item in entries if (not level or str(item.get("level", "")).casefold() == level)
               and (not trace_id or str(item.get("trace_id", "")) == trace_id)
               and (not correlation_id or str(item.get("correlation_id", "")) == correlation_id)
               and (not request_id or str(item.get("request_id", "")) == request_id)
               and (not service or str(item.get("service", "")) == service)
               and (since is None or (_timestamp_seconds(item.get("timestamp")) or 0) >= since)
               and (until is None or (_timestamp_seconds(item.get("timestamp")) or 0) <= until)
               and _entry_visible(request, item, requested_chat_id)]
    entries = _bounded_entries([_ops_redact(item) for item in (entries[-limit:] if cursor == 0 else entries[:limit])])
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
    return JsonResponse({
        "provider": tracing.trace_provider(),
        "enabled": tracing.enabled(),
        "context": _ops_redact(context.snapshot()),
    })


@require_http_methods(["GET"])
@_auth
def ops_trace_detail(request, trace_id):
    if (blocked := _guard(request, "trace")):
        return blocked
    wanted = str(trace_id)[:64]
    entries = _bounded_entries([
        _ops_redact(item)
        for item in obs_logging.RING.tail(500)
        if str(item.get("trace_id", "")) == wanted and _entry_visible(request, item)
    ])
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
    if len(request.body or b"") > 16_384:
        return JsonResponse({"error": "request body too large"}, status=413)
    body = _json(request)
    if body is None:
        return JsonResponse({"error": "request body must be a JSON object"}, status=400)
    unknown_fields = set(body) - {"test_id"}
    if unknown_fields:
        return JsonResponse({"error": "unknown fields: " + ", ".join(sorted(map(str, unknown_fields)))}, status=400)
    test_id = str((body or {}).get("test_id", ""))
    check = SAFE_TESTS.get(test_id)
    if check is None:
        ops_auth_failure("unknown_diagnostic")
        return JsonResponse({"error": "unknown test_id", "tests": sorted(SAFE_TESTS)}, status=400)
    run_id = uuid.uuid4().hex
    started_at = time.time()
    record = {
        "run_id": run_id,
        "test_id": test_id,
        "status": "running",
        "outcome": "pending",
        "result": {},
        "started_at": started_at,
        "completed_at": None,
        "events": [],
    }
    _append_run_event(record, "diagnostic.started", "running")
    result, timed_out, saturated = _execute_check(check)
    result = _bounded_result(result)
    duration_ms = round((time.time() - started_at) * 1000, 2)
    if saturated:
        status, outcome = "rejected", "failed"
        _append_run_event(
            record, "diagnostic.rejected", "failed",
            error_type=result.get("error_type"), duration_ms=duration_ms,
        )
    elif timed_out:
        status, outcome = "timed_out", "failed"
        _append_run_event(
            record, "diagnostic.timeout", "failed",
            error_type="TimeoutError", duration_ms=duration_ms,
        )
    else:
        status, outcome = "completed", "ok" if result.get("ok") else "failed"
        _append_run_event(record, "diagnostic.completed", outcome, duration_ms=duration_ms)
    ops_diagnostic_run(test_id=test_id, group="ops", outcome=outcome)
    obs_logging.emit(
        "ops.diagnostic_run",
        level="info" if outcome == "ok" else "warning",
        test_id=test_id,
        outcome=outcome,
        status=status,
    )
    record.update({
        "status": status,
        "outcome": outcome,
        "result": result,
        "completed_at": time.time(),
        "duration_ms": duration_ms,
    })
    with _RUNS_LOCK:
        _RUNS[run_id] = record
        while len(_RUNS) > _MAX_RUNS:
            _RUNS.pop(next(iter(_RUNS)))
    if saturated:
        return JsonResponse({"error": "diagnostic concurrency limit reached", "run_id": run_id}, status=429)
    response = {
        "test_id": test_id,
        "run_id": run_id,
        "status": status,
        "outcome": outcome,
        "result": result,
    }
    return JsonResponse(response, status=200 if status == "completed" else 504 if timed_out else 429)


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


@require_http_methods(["GET"])
@_auth
def ops_diagnostic_run_events(request, run_id):
    if (blocked := _guard(request, "diagnostic_result")):
        return blocked
    with _RUNS_LOCK:
        record = _RUNS.get(str(run_id))
    if record is None:
        return JsonResponse({"error": "run not found"}, status=404)
    return JsonResponse({"run_id": str(run_id), "events": _ops_redact(record.get("events", []))})


@require_http_methods(["GET"])
@_auth
def ops_diagnostic_run_report(request, run_id):
    if (blocked := _guard(request, "diagnostic_result")):
        return blocked
    with _RUNS_LOCK:
        record = _RUNS.get(str(run_id))
    if record is None:
        return JsonResponse({"error": "run not found"}, status=404)
    report = {
        "run_id": record["run_id"],
        "test_id": record["test_id"],
        "status": record["status"],
        "outcome": record["outcome"],
        "started_at": record["started_at"],
        "completed_at": record["completed_at"],
        "duration_ms": record.get("duration_ms"),
        "result": record["result"],
        "event_count": len(record.get("events", [])),
    }
    return JsonResponse(_ops_redact(report))


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
