"""Credential store: env-only, redacted everywhere.

AI never sees these values. Tools call connectors; connectors read the
store. Nothing here is ever placed into prompts, tool results, logs
beyond boolean presence, or audit payloads.
"""
from __future__ import annotations

import os


def _get(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


def _flag(name: str, default: str = "false") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


class CredentialStore:
    @staticmethod
    def x_enabled() -> bool:
        return _flag("X_RESEARCH_ENABLED", "false")

    @staticmethod
    def x_provider() -> str:
        v = _get("X_PROVIDER").lower() or "official"
        return v if v in {"official", "session", "auto"} else "official"

    @staticmethod
    def x_bearer() -> str:
        return _get("X_BEARER_TOKEN")

    @staticmethod
    def x_api_key() -> str:
        return _get("X_API_KEY")

    @staticmethod
    def x_api_secret() -> str:
        return _get("X_API_SECRET")

    @staticmethod
    def x_access_token() -> str:
        return _get("X_ACCESS_TOKEN")

    @staticmethod
    def x_access_secret() -> str:
        return _get("X_ACCESS_TOKEN_SECRET")

    @staticmethod
    def x_session_enabled() -> bool:
        return _flag("X_SESSION_ENABLED", "false")

    @staticmethod
    def x_bridge_url() -> str:
        return _get("X_BRIDGE_URL") or "http://127.0.0.1:8791"

    @staticmethod
    def x_bridge_token() -> str:
        return _get("X_BRIDGE_INTERNAL_TOKEN")

    @staticmethod
    def youtube_enabled() -> bool:
        return _flag("YOUTUBE_RESEARCH_ENABLED", "false")

    @staticmethod
    def youtube_api_key() -> str:
        return _get("YOUTUBE_API_KEY")

    @staticmethod
    def youtube_client_id() -> str:
        return _get("YOUTUBE_CLIENT_ID")

    @staticmethod
    def youtube_client_secret() -> str:
        return _get("YOUTUBE_CLIENT_SECRET")

    @staticmethod
    def youtube_refresh_token() -> str:
        return _get("YOUTUBE_REFRESH_TOKEN")


def has_secret(value: str, text: str) -> bool:
    """True if a non-trivial credential value leaks into text."""
    if not value or len(value) < 8:
        return False
    return value in (text or "")


def assert_no_credentials(text: str) -> None:
    """Fail-closed guard used in tests and before model context assembly."""
    for secret in (
        CredentialStore.x_bearer(),
        CredentialStore.x_api_key(),
        CredentialStore.x_api_secret(),
        CredentialStore.x_access_token(),
        CredentialStore.x_access_secret(),
        CredentialStore.x_bridge_token(),
        CredentialStore.youtube_api_key(),
        CredentialStore.youtube_client_secret(),
        CredentialStore.youtube_refresh_token(),
    ):
        if has_secret(secret, text):
            raise ValueError("credential leak detected in model-bound text")
