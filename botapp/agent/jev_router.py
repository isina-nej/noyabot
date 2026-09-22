"""Jev (SystemOne) fast-path router: admin-ops vs ordinary Noya chat.

Fail-open by design: any problem (disabled flag, missing key, timeout,
bad response, dead subscription/quota) returns None and the caller falls
through to the existing LLM classifier, then to Noya chat. Jev must never
break Noya.

Env:
  JEV_ENABLED=true|false        master switch (default true)
  JEV_MODEL                     primary model (default oc/jev-1.13-free)
  JEV_FALLBACK_MODEL            one paid fallback (default
                                openrouter/typesafe/jev-1.13; empty = none)
  JEV_TIMEOUT                   seconds per attempt (default 6)
  JEV_COOLDOWN_SECONDS          skip Jev this long after all models fail
                                (default 120; 0 = no cooldown)
  JEV_API_URL                   override; default derives /systemone from
                                NOYA_API_URL
Auth reuses NOYA_API_KEY. Empty key = disabled (also avoids httpx
crashing on an empty `Bearer ` header).
"""

from __future__ import annotations

import logging
import os
import time

import httpx

logger = logging.getLogger("botapp.agent")

_DEFAULT_TIMEOUT = 6.0
_DEFAULT_COOLDOWN = 120.0

# Timestamp (monotonic) until which Jev is skipped after all models failed.
# Prevents every new message from paying timeout x models when dead.
_cool_until = 0.0

_CRITERIA = {
    "agent": (
        "ban/mute/unmute/warn/lock/unlock/delete/pin/group stats/member "
        "analytics/admin settings/schedules/admin investigation"
    ),
    "chat": (
        "ordinary question, joke, greeting, weather, opinion, or anything "
        "needing no Telegram admin tool"
    ),
}


def jev_enabled() -> bool:
    if os.getenv("JEV_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return False
    return bool(os.getenv("NOYA_API_KEY", "").strip())


def _systemone_url() -> str:
    explicit = os.getenv("JEV_API_URL", "").strip()
    if explicit:
        return explicit
    base = os.getenv(
        "NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions"
    ).strip()
    if "/chat/completions" in base:
        return base.replace("/chat/completions", "/systemone")
    return base.rstrip("/") + "/systemone"


def _models() -> list[str]:
    primary = os.getenv("JEV_MODEL", "oc/jev-1.13-free").strip() or "oc/jev-1.13-free"
    fallback = os.getenv("JEV_FALLBACK_MODEL", "openrouter/typesafe/jev-1.13").strip()
    models = [primary]
    if fallback and fallback != primary:
        models.append(fallback)
    return models


def _timeout() -> float:
    try:
        return max(1.0, float(os.getenv("JEV_TIMEOUT", str(_DEFAULT_TIMEOUT))))
    except ValueError:
        return _DEFAULT_TIMEOUT


def _cooldown() -> float:
    try:
        return max(0.0, float(os.getenv("JEV_COOLDOWN_SECONDS", str(_DEFAULT_COOLDOWN))))
    except ValueError:
        return _DEFAULT_COOLDOWN


def _cooling_down() -> bool:
    global _cool_until
    return _cool_until > time.monotonic()


def _mark_all_failed() -> None:
    global _cool_until
    wait = _cooldown()
    if wait > 0:
        _cool_until = time.monotonic() + wait


async def _ask(
    model: str, url: str, headers: dict, text: str, timeout: float
) -> dict | None:
    payload = {
        "model": model,
        "state": (text or "")[:4000],
        "questions": {
            "route": {
                "type": "choice",
                "instructions": (
                    "Is this Telegram message an admin management command "
                    "or ordinary chat?"
                ),
                "criteria": _CRITERIA,
            }
        },
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            answer = resp.json()["answers"]["route"]
    except Exception as exc:
        logger.warning("jev_router model=%s failed: %s", model, type(exc).__name__)
        return None
    try:
        route = str(answer.get("choice") or answer.get("route") or "").strip().lower()
        conf = float(answer.get("confidence") or 0.0)
    except (TypeError, ValueError, AttributeError):
        logger.warning("jev_router model=%s bad shape", model)
        return None
    if route not in {"agent", "chat"}:
        logger.warning("jev_router model=%s unknown route=%r", model, route)
        return None
    return {"route": route, "confidence": conf, "thinking": "", "reason": "jev"}


async def jev_classify_route(text: str, *, chat_id: int) -> dict | None:
    """Fast SystemOne verdict, or None when Jev has no opinion (fail-open)."""
    if not jev_enabled():
        return None
    if _cooling_down():
        return None
    stripped = (text or "").strip()
    if not stripped:
        return None
    headers = {
        "Authorization": f"Bearer {os.getenv('NOYA_API_KEY', '').strip()}",
        "Content-Type": "application/json",
    }
    url = _systemone_url()
    timeout = _timeout()
    for model in _models():
        verdict = await _ask(model, url, headers, stripped, timeout)
        if verdict is not None:
            logger.info(
                "jev_route chat=%s route=%s conf=%.2f model=%s",
                chat_id,
                verdict["route"],
                verdict["confidence"],
                model,
            )
            return verdict
    _mark_all_failed()
    return None
