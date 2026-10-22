"""Pseudonymous, low-cardinality correlation context for Noya telemetry.

Single source of truth for every correlation id used by logs, traces,
metrics and the ops API.  Contextvars survive async task boundaries (the
aiogram handler chain, agent loop, tool dispatch), which is what makes a
Telegram update traceable end-to-end.

Privacy invariants:
* Raw Telegram ids NEVER enter telemetry.  ``scope_ref`` is a keyed,
  truncated SHA-256 of ``(chat_id, user_id)`` — stable for correlation,
  useless for identification without the server-side secret.
* Message bodies, prompts and reasoning content never pass through here.
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timezone

# ── contextvars ────────────────────────────────────────────────────────────
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "noya_correlation_id", default=""
)
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "noya_trace_id", default=""
)
span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "noya_span_id", default=""
)
update_id_var: contextvars.ContextVar[int] = contextvars.ContextVar(
    "noya_update_id", default=0
)
message_id_var: contextvars.ContextVar[int] = contextvars.ContextVar(
    "noya_message_id", default=0
)
scope_ref_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "noya_scope_ref", default=""
)
operation_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "noya_operation", default=""
)
# Optional OpenTelemetry ids mirrored for plain-log access without an SDK dep.
otel_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "noya_otel_trace_id", default=""
)
otel_span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "noya_otel_span_id", default=""
)

_OTEL_TRACE_LEN = 32  # 128-bit
_OTEL_SPAN_LEN = 16  # 64-bit

_PSEUDO_KEY_CACHE: str | None = None


def _pseudo_key() -> str:
    """Server-local pepper for pseudonymous ids; never exported."""
    global _PSEUDO_KEY_CACHE
    if _PSEUDO_KEY_CACHE is None:
        _PSEUDO_KEY_CACHE = os.getenv("NOYA_TELEMETRY_HASH_KEY", "") or "noya-dev-pepper"
    return _PSEUDO_KEY_CACHE


def scope_ref(chat_id: int | str | None, user_id: int | str | None) -> str:
    """Keyed pseudonymous identifier replacing (chat, user) raw ids."""
    if chat_id is None and user_id is None:
        return ""
    payload = f"{chat_id}:{user_id}:{_pseudo_key()}".encode("utf-8", "ignore")
    return hashlib.sha256(payload).hexdigest()[:12]


def new_correlation_id() -> str:
    """Opaque random id for one logical request (spawns, retries included)."""
    return uuid.uuid4().hex[:12]


def new_trace_id() -> str:
    """64-hex W3C-style trace id."""
    return secrets.token_hex(16)


def reset() -> None:
    """Clear every field for test isolation (the worker re-binds per update)."""
    correlation_id_var.set("")
    trace_id_var.set("")
    span_id_var.set("")
    update_id_var.set(0)
    message_id_var.set(0)
    scope_ref_var.set("")
    operation_var.set("")
    otel_trace_id_var.set("")
    otel_span_id_var.set("")


def utc_now_iso() -> str:
    """UTC timestamp with milliseconds for logging schema."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def otel_safe_trace_id(value: str) -> str:
    """Coerce any correlation id to an OTel-legal 32-hex trace id."""
    value = (value or "").strip()
    if len(value) == _OTEL_TRACE_LEN and all(c in "0123456789abcdef" for c in value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()
    return digest[:_OTEL_TRACE_LEN]


def otel_safe_span_id(value: str) -> str:
    value = (value or "").strip()
    if len(value) == _OTEL_SPAN_LEN and all(c in "0123456789abcdef" for c in value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()
    return digest[:_OTEL_SPAN_LEN]


def bind(
    *,
    correlation_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    update_id: int = 0,
    message_id: int = 0,
    chat_id: int | None = None,
    user_id: int | None = None,
    operation: str = "",
) -> None:
    """Bind correlation fields to the current context (never raw ids)."""
    if correlation_id:
        correlation_id_var.set(correlation_id)
    if trace_id:
        trace_id_var.set(trace_id)
    if span_id:
        span_id_var.set(span_id)
    if update_id:
        update_id_var.set(int(update_id))
    if message_id:
        message_id_var.set(int(message_id))
    if chat_id is not None or user_id is not None:
        scope_ref_var.set(scope_ref(chat_id, user_id))
    if operation:
        operation_var.set(operation)


def snapshot() -> dict:
    """Flatten correlation fields for the current context."""
    return {
        "correlation_id": correlation_id_var.get() or None,
        "trace_id": trace_id_var.get() or None,
        "span_id": span_id_var.get() or None,
        "otel_trace_id": otel_trace_id_var.get() or None,
        "otel_span_id": otel_span_id_var.get() or None,
        "telegram_update_id": update_id_var.get() or None,
        "message_id": message_id_var.get() or None,
        "conversation_scope": scope_ref_var.get() or None,
        "operation": operation_var.get() or None,
    }
