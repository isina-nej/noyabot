"""AI-first router: admin-ops vs ordinary Noya chat.

Fail-open: if the classifier is unavailable, the message stays in Noya chat
instead of becoming a fake admin-command error.
"""

from __future__ import annotations

import logging
import os

from django.conf import settings as dj_settings

from .ai import NoyaAgentProvider
from .cache import cache_key, classify_cache
from .parser import parse as deterministic_parse

logger = logging.getLogger("botapp.agent")

_MIN_ROUTE_CONFIDENCE = 0.72

# Jev fast-path thresholds: only a confident Jev verdict short-circuits the
# LLM classifier. Anything ambiguous falls through to the existing path.
_JEV_AGENT_CONFIDENCE = 0.80
_JEV_CHAT_CONFIDENCE = 0.70


def _normalize(text: str) -> str:
    return " ".join((text or "").casefold().split())


async def should_route_to_agent(text: str, *, chat_id: int, provider=None) -> bool:
    """Return True only when the message is a real admin/ops request."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if deterministic_parse(stripped) is not None:
        return True
    if not bool(getattr(dj_settings, "AGENT_CLASSIFY_ENABLED", True)):
        return False
    if not bool(getattr(dj_settings, "AGENT_AI_ENABLED", True)):
        return False

    key = cache_key("classify", chat_id, _normalize(stripped))
    cached = classify_cache.get(key)
    if cached is not None:
        return bool(cached)

    # ── Jev fast path (SystemOne, fail-open) ──
    # Returns None on any failure; confident verdicts short-circuit,
    # everything else falls through to the LLM classifier below.
    try:
        from .jev_router import jev_classify_route

        jev = await jev_classify_route(stripped, chat_id=chat_id)
    except Exception:
        logger.info("jev_classify_fail_open chat=%s", chat_id)
        jev = None
    if jev is not None:
        route, conf = jev.get("route"), float(jev.get("confidence") or 0.0)
        if route == "agent" and conf >= _JEV_AGENT_CONFIDENCE:
            logger.info("jev_route_fast chat=%s agent conf=%.2f", chat_id, conf)
            classify_cache.set(key, True)
            return True
        if route == "chat" and conf >= _JEV_CHAT_CONFIDENCE:
            logger.info("jev_route_fast chat=%s chat conf=%.2f", chat_id, conf)
            classify_cache.set(key, False)
            return False
        logger.info(
            "jev_route_ambiguous chat=%s route=%s conf=%.2f llm_fallback",
            chat_id,
            route,
            conf,
        )

    timeout = float(os.getenv("AGENT_CLASSIFY_TIMEOUT", "2.5"))
    client = provider or NoyaAgentProvider(timeout=timeout)
    try:
        decision = await client.classify_route(stripped, chat_id=chat_id)
    except Exception:
        logger.info("agent_classify_fail_open chat=%s", chat_id)
        classify_cache.set(key, False)
        return False

    thinking = (decision.get("thinking") or "").strip()
    if thinking:
        logger.info("agent_thinking chat=%s %s", chat_id, thinking[:240])
    route = (decision.get("route") or "chat").strip().lower()
    try:
        confidence = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    ok = route == "agent" and confidence >= _MIN_ROUTE_CONFIDENCE
    logger.info(
        "agent_classify chat=%s route=%s conf=%.2f ok=%s",
        chat_id,
        route,
        confidence,
        ok,
    )
    classify_cache.set(key, ok)
    return ok
