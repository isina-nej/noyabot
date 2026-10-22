"""Isolated operator authentication.

Ordinary moderation StaffAPIKey values are deliberately NOT accepted as ops
credentials. Operator tokens are provided by the deployment secret manager and
can be rotated without a database migration.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, deque
from functools import wraps

from django.views.decorators.csrf import csrf_exempt

from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware

from .instrumentation import ops_auth_failure

_FAIL_LOCK = threading.Lock()
_FAILS = defaultdict(deque)
_WINDOW = 60
_FAIL_LIMIT = 20


def _throttled(request):
    ip = request.META.get("REMOTE_ADDR", "unknown")[:80]
    now = time.monotonic()
    with _FAIL_LOCK:
        recent = _FAILS[ip]
        while recent and now - recent[0] > _WINDOW:
            recent.popleft()
        if len(recent) >= _FAIL_LIMIT:
            return True
        recent.append(now)
    return False


def _match(candidate: str, configured: str) -> bool:
    # Short/placeholder secrets are unsafe and must not be accepted.
    if not configured or len(configured) < 32 or len(candidate) > 4096:
        return False
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(configured.encode()).digest(),
    )


def _token_scope(raw: str) -> str:
    if not raw:
        return ""
    for key in ("NOYA_OPS_RUN_TOKEN", "NOYA_OPS_RUN_TOKEN_PREVIOUS"):
        if _match(raw, os.getenv(key, "")):
            return "run"
    for key in ("NOYA_OPS_READ_TOKEN", "NOYA_OPS_READ_TOKEN_PREVIOUS"):
        if _match(raw, os.getenv(key, "")):
            return "read"
    return ""


def ops_authenticated(view):
    """Read endpoints accept read/run token; POST tests require run token.

    A Django superuser session is also allowed, but CSRF remains mandatory
    for POST. Staff users and moderation API keys are *not* ops administrators.
    """
    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        scope = "run" if request.method not in ("GET", "HEAD", "OPTIONS") else "read"
        header = request.headers.get("X-Noya-Ops-Key", "") or request.headers.get("X-API-Key", "")
        token_scope = _token_scope(header)
        if token_scope and (scope == "read" or token_scope == "run"):
            request.ops_actor = "ops-token"
            request.ops_scope = token_scope
            return view(request, *args, **kwargs)
        if token_scope == "read" and scope == "run":
            ops_auth_failure("insufficient_scope")
            return JsonResponse({"error": "run permission required"}, status=403)
        if request.user.is_authenticated and request.user.is_superuser:
            csrf_failure = CsrfViewMiddleware(lambda _request: None).process_view(request, view, args, kwargs)
            if csrf_failure:
                return JsonResponse({"error": "CSRF verification failed"}, status=403)
            request.ops_actor = "superuser"
            request.ops_scope = "run"
            return view(request, *args, **kwargs)
        ops_auth_failure("unauthorized")
        if _throttled(request):
            return JsonResponse({"error": "too many authentication failures"}, status=429)
        return JsonResponse({"error": "ops authentication required"}, status=401)
    return wrapped
