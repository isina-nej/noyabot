"""JEV-first router: the model selects the route, deterministic only validates.

Fail-open: if the classifier is unavailable, the message stays in Noya chat
instead of becoming a fake admin-command error.

``deterministic_parse`` is never the primary selector here. Route selection
is: JEV SystemOne 4-way verdict → 9Router structured JSON verdict → legacy
binary verdict → deterministic last-resort validation (see
``botapp.agent.route_classifier``). Only a confident admin/harness verdict
(confidence >= 0.80) routes to the agent; chat/media/low-confidence verdicts
stay in Noya chat, where the existing media/analysis handling applies.
"""

from __future__ import annotations

import logging
import os

from django.conf import settings as dj_settings

from .ai import NoyaAgentProvider
from .cache import cache_key, classify_cache

logger = logging.getLogger("botapp.agent")


def _normalize(text: str) -> str:
    return " ".join((text or "").casefold().split())


async def should_route_to_agent(
    text: str, *, chat_id: int, provider=None, has_reply: bool = False
) -> bool:
    """Return True only for a confident admin/harness routing verdict."""
    stripped = (text or "").strip()
    if not stripped:
        return False

    # Classifier disabled: deterministic validation only (offline fallback).
    # This is validation of last resort, not primary selection.
    if not bool(getattr(dj_settings, "AGENT_CLASSIFY_ENABLED", True)):
        from .parser import parse as deterministic_parse

        return deterministic_parse(stripped) is not None
    if not bool(getattr(dj_settings, "AGENT_AI_ENABLED", True)):
        from .parser import parse as deterministic_parse

        return deterministic_parse(stripped) is not None

    key = cache_key("classify", chat_id, _normalize(stripped))
    cached = classify_cache.get(key)
    if cached is not None:
        return bool(cached)

    # ── JEV-first 4-way verdict (SystemOne, fail-open) ──
    # Model-first: JEV → 9Router structured JSON → legacy binary →
    # deterministic last-resort. Only confident admin/harness routes to agent.
    from .route_classifier import classify_request, is_tool_execution_allowed

    timeout = float(os.getenv("AGENT_CLASSIFY_TIMEOUT", "2.5"))
    client = provider or NoyaAgentProvider(timeout=timeout)
    try:
        verdict = await classify_request(
            stripped,
            chat_id=chat_id,
            provider=client,
            has_reply=has_reply,
            use_jev=True,
        )
    except Exception:
        logger.info("route_classify_fail_open chat=%s", chat_id)
        verdict = None

    if verdict is None:
        # Total fallback: deterministic validation only, never selection.
        from .parser import parse as deterministic_parse

        ok = deterministic_parse(stripped) is not None
        logger.info("route_fallback chat=%s ok=%s", chat_id, ok)
        classify_cache.set(key, ok)
        return ok

    ok = bool(is_tool_execution_allowed(verdict))
    logger.info(
        "route_verdict chat=%s route=%s conf=%.2f ok=%s",
        chat_id,
        verdict.route,
        verdict.confidence,
        ok,
    )
    classify_cache.set(key, ok)
    return ok
