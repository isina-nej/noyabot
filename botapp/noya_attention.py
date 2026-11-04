"""Cheap, context-safe acknowledgement for a bare call of Noya's name.

A mere vocative is not an instruction to answer somebody else's earlier
question. Avoid an expensive, context-heavy LLM turn and minute-long notices.
"""
from __future__ import annotations

import os
import re


_BARE_CALL = re.compile(
    r"^(?:نویا|نُویا|noya|nuya|@NuyaRobot|@iNuyaRobot)\s*[!؟?،,.\u061f\s]*$",
    re.IGNORECASE,
)


def quick_attention_reply(message) -> str | None:
    enabled = os.getenv("NOYA_FAST_ACK_ENABLED", "true").strip().casefold()
    if enabled not in {"true", "1", "yes", "on"}:
        return None
    user = getattr(message, "from_user", None)
    if user is None or getattr(user, "is_bot", False):
        return None
    if getattr(message, "reply_to_message", None) is not None:
        # A reply can mean "look at this"; preserve its context.
        return None
    if any(getattr(message, name, None) for name in (
        "photo", "sticker", "animation", "video", "voice", "audio",
        "video_note", "document",
    )):
        return None
    text = str(getattr(message, "text", "") or "").strip()
    return "جانم، بگو ببینم چی شده؟" if _BARE_CALL.fullmatch(text) else None
