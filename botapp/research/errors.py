"""Normalized error taxonomy for research providers."""
from __future__ import annotations


class ResearchError(Exception):
    code = "provider_error"


class AuthRequired(ResearchError):
    code = "auth_required"


class RateLimited(ResearchError):
    code = "rate_limited"


class NotFound(ResearchError):
    code = "not_found"


class ProviderTimeout(ResearchError):
    code = "timeout"


class ProviderError(ResearchError):
    code = "provider_error"


class CapabilityUnavailable(ResearchError):
    code = "capability_unavailable"


class ResearchDisabled(ResearchError):
    code = "disabled"


def normalize_error(exc: BaseException) -> str:
    if isinstance(exc, ResearchError):
        return exc.code
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "auth" in name or "401" in msg or "403" in msg or "unauthorized" in msg:
        return "auth_required"
    if "rate" in msg or "429" in msg or "quota" in msg or "limit" in msg:
        return "rate_limited"
    if "timeout" in name or "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "not found" in msg or "404" in msg:
        return "not_found"
    return "provider_error"
