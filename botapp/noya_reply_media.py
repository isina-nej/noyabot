"""Deterministic Telegram reply command routing for Noya media.

Never paraphrase or translate quoted image prompts: the user has explicitly
selected a source message. Commands do not grant additional permissions.
"""
from __future__ import annotations

import re

_REPLY_IMAGE = frozenset({"عکس", "تصویر", "عکس پرامپت", "تصویر پرامپت", "image", "draw", "پرامپت عکس"})
_REPLY_VOICE = frozenset({"ویس", "صدا", "صوت", "صوتی", "voice", "tts", "ویس کن", "صدا کن", "ویس بگو", "ویس بده"})
_REPLY_TEXT = frozenset({"متن", "تبدیل به متن", "متن کن", "متن ویس", "متن صدا", "رونوشت", "transcribe", "text"})
_REPLY_STICKER = frozenset({"استیکر", "استیکرش کن", "استیکر کن", "تبدیل به استیکر", "عکس به استیکر", "sticker"})
_SHORT = re.compile(r"\s+")


def classify_reply_command(ask: str, *, has_reply: bool) -> str | None:
    """Route only explicit short commands, never broad word inclusion."""
    if not has_reply:
        return None
    cmd = _SHORT.sub(" ", (ask or "").casefold().strip(" \t\n\r!؟?.,،:")).strip()
    if cmd in _REPLY_IMAGE or cmd in {
        "با این متن عکس بساز", "از این متن عکس بساز", "عکس همین پرامپت",
        "عکس پرامپت این پیام", "این متن رو عکس کن",
    }:
        return "image"
    if cmd in _REPLY_VOICE or cmd in {
        "این متن رو ویس کن", "این پیام رو ویس کن", "متن این پیام رو بخون",
        "این متنو ویس کن", "این رو ویس کن",
    }:
        return "voice"
    if cmd in _REPLY_TEXT or cmd in {
        "این ویس رو متن کن", "این صدا رو متن کن", "متنش کن",
        "متن این ویس", "پیاده کن",
    }:
        return "text"
    if cmd in _REPLY_STICKER or cmd in {
        "این عکس رو استیکر کن", "عکس رو استیکر کن", "از این عکس استیکر بساز",
        "این عکسو استیکر کن", "تبدیلش کن به استیکر",
        "از این تصویر استیکر بساز",
    }:
        return "sticker"
    return None


def selected_reply_text(message) -> str:
    """Use only the message actually replied to, not adjacent group chatter."""
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return ""
    from botapp.telegram_rich import extract_message_body

    body = extract_message_body(reply)
    return (body or "").strip()[:12_000]


def explicit_image_prompt(ask: str, *, has_reply: bool) -> str | None:
    """'عکس X' is a direct prompt, but bare 'عکس' requires replied text."""
    text = (ask or "").strip()
    if not text or has_reply:
        return None
    m = re.fullmatch(r"(?:عکس|تصویر|image)\s*[:،-]?\s+(.+)", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    rest = m.group(1).strip()
    return rest if len(rest) >= 3 and rest not in {"پرامپت", "پرومت"} else None
