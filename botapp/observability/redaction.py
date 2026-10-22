"""Credential/PII redaction shared by logs, audit payloads and API output."""
from __future__ import annotations

import re

_SECRET_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "key:",
    "credential",
    "cookie",
    "session",
    "seed",
    "bearer",
    "prompt",
    "raw_text",
    "input_text",
    "message_text",
    "caption",
    "reasoning",
    "thinking",
    "query",
)

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)^\s*(?:bot_?token|api_?key|authorization|password|secret|"
    r"refresh_?token|access_?token|session_?cookie|private_?key|x-api-key)$"
)

# Authorization/cookie shapes inside arbitrary strings.
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_URLSAFE_RE = re.compile(r"\b[A-Za-z0-9_-]{30,}\b")
_HEX64_RE = re.compile(r"\b[a-fA-F0-9]{40,}\b")
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")
_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/=]{80,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")


def redact_string(value: str, *, max_length: int = 200) -> str:
    """Redact credential shapes inside a free string (bounded)."""
    text = str(value)[: max(max_length, 20)]
    # Keep shape based scrubbing here (rather than only key based scrubbing):
    # provider errors and URLs routinely carry credentials under arbitrary keys.
    for pattern in (_JWT_RE, _TELEGRAM_TOKEN_RE, _BEARER_RE,
                    _HEX64_RE, _LONG_B64_RE, _TOKEN_URLSAFE_RE):
        text = pattern.sub("[REDACTED]", text)
    if _SENSITIVE_KEY_RE.match(text):
        return "[REDACTED]"
    return text


def redact_value(value, *, depth: int = 0):
    """Depth-limited value redaction for dicts/lists/scalars."""
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            folded = str(key).casefold()
            if any(hint in folded for hint in _SECRET_HINTS) and not str(key).startswith("__"):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_value(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item, depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_string(str(value))


def redact_text_keywords(values: dict) -> dict:
    """Apply :data:`redact_value` to a flat payload (convenience wrapper)."""
    return redact_value(values)
