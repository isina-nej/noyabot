"""Jev (SystemOne) media router: voice vs image vs plain text.

Fail-open by design: any problem (disabled flag, missing key, timeout,
bad response, dead subscription/quota) returns None and the caller falls
through to the existing LLM path. Jev must never break Noya.

Why Jev here: the LLM currently guesses modality from the system prompt.
A closed 3-way choice (voice/image/none) is exactly what SystemOne is
for — cheap, fast, no hallucinated structure.

Cost guard: messages with no media hint word skip Jev entirely (no HTTP),
so plain chat pays zero extra latency.

Env:
  JEV_MEDIA_ENABLED=true|false   kill-switch for this router (default true;
                                 JEV_ENABLED=false still disables everything)
  JEV_VOICE_CONFIDENCE           min confidence to force TTS (default 0.85;
                                 voice stays strict: speaking the wrong text
                                 aloud is worse than a missed image)
  JEV_IMAGE_CONFIDENCE           min confidence to force image gen (default 0.75)
Auth/timeout/models/cooldown are shared with jev_router (same key, breaker).
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from .jev_router import (
    _cooling_down,
    _mark_all_failed,
    _models,
    _systemone_url,
    _timeout,
    jev_enabled,
)

logger = logging.getLogger("botapp.agent")

_MEDIA_CRITERIA = {
    "voice": (
        "user explicitly asks for the answer as a voice/audio message "
        "(e.g. ویس بده, با صدا بگو, صوتی کن, voice, TTS)"
    ),
    "image": (
        "user asks to draw, generate, or edit a picture "
        "(e.g. عکس بکش, نقاشی بساز, تصویر درست کن, draw, edit this photo)"
    ),
    "none": (
        "ordinary text question, greeting, joke, admin command, or anything "
        "needing no voice message and no generated image"
    ),
}

# Cheap pre-filter: without one of these substrings Jev cannot say
# voice/image anyway, so plain chat never pays an HTTP call.
_MEDIA_HINT_RE = re.compile(
    r"ویس|ویسی|وویس|صوتی|صوت|صدا|voice|tts|عکس|تصویر|نقاشی|draw|paint|image|picture|بکش|بساز",
    re.IGNORECASE,
)

_VOICE_STRIP_RE = re.compile(
    r"ویس|ویسی|وویس|صوتی|صوت|صدا|voice|tts|بگو|بده|بکن|بفرست|بخون|بخوان|بشنو|بساز|کن|لطفا|لطفاً|برام|واسم|میخوام|می‌خوام|با|رو|را|اینو|این",
    re.IGNORECASE,
)
_IMAGE_STRIP_RE = re.compile(
    r"عکس|عکسو|تصویر|نقاشی|بکش|بکشی|بکشش|بساز|بسازی|بسازش|تولید|درست|ادیت|ویرایش|draw|paint|generate|image|picture|photo|pic|لطفا|لطفاً|برام|واسم|یه|یک|از|رو|را|کن|بکن",
    re.IGNORECASE,
)


def jev_media_enabled() -> bool:
    if os.getenv("JEV_MEDIA_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return False
    return jev_enabled()


def _voice_threshold() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("JEV_VOICE_CONFIDENCE", "0.85"))))
    except ValueError:
        return 0.85


def _image_threshold() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("JEV_IMAGE_CONFIDENCE", "0.75"))))
    except ValueError:
        return 0.75


def media_action(media: str | None, confidence) -> str | None:
    """Map a Jev verdict to 'voice'/'image'/None after confidence gating."""
    try:
        conf = float(confidence or 0.0)
    except (TypeError, ValueError):
        return None
    if media == "voice" and conf >= _voice_threshold():
        return "voice"
    if media == "image" and conf >= _image_threshold():
        return "image"
    return None


def strip_media_trigger(text: str, kind: str) -> str | None:
    """Best-effort content recovery when Jev is sure but the strict regex missed.

    Returns None when nothing usable remains (caller falls back to LLM).
    """
    t = (text or "").strip()
    if not t:
        return None
    pat = _VOICE_STRIP_RE if kind == "voice" else _IMAGE_STRIP_RE
    rest = pat.sub(" ", t)
    rest = re.sub(r"\s+", " ", rest).strip(" \t،,.:!-؟?\"'«»")
    return rest if len(rest) >= 3 else None


async def _ask_media(model: str, url: str, headers: dict, text: str, timeout: float) -> dict | None:
    payload = {
        "model": model,
        "state": (text or "")[:4000],
        "questions": {
            "media": {
                "type": "choice",
                "instructions": (
                    "Does this Telegram message to Noya request a voice "
                    "message, an image, or neither?"
                ),
                "criteria": _MEDIA_CRITERIA,
            }
        },
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            answer = resp.json()["answers"]["media"]
    except Exception as exc:
        logger.warning("jev_media model=%s failed: %s", model, type(exc).__name__)
        return None
    try:
        media = str(answer.get("choice") or answer.get("media") or "").strip().lower()
        conf = float(answer.get("confidence") or 0.0)
    except (TypeError, ValueError, AttributeError):
        logger.warning("jev_media model=%s bad shape", model)
        return None
    if media not in {"voice", "image", "none"}:
        logger.warning("jev_media model=%s unknown media=%r", model, media)
        return None
    return {"media": media, "confidence": conf}


async def jev_classify_media(text: str, *, chat_id: int) -> dict | None:
    """Fast SystemOne media verdict, or None when Jev has no opinion (fail-open)."""
    if not jev_media_enabled():
        return None
    if _cooling_down():
        return None
    stripped = (text or "").strip()
    if not stripped or not _MEDIA_HINT_RE.search(stripped):
        return None
    api_key = os.getenv("NOYA_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = _systemone_url()
    timeout = _timeout()
    for model in _models():
        verdict = await _ask_media(model, url, headers, stripped, timeout)
        if verdict is not None:
            logger.info(
                "jev_media chat=%s media=%s conf=%.2f model=%s",
                chat_id,
                verdict["media"],
                verdict["confidence"],
                model,
            )
            return verdict
    _mark_all_failed()
    return None
