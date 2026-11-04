"""Application-side eligibility for deliberate silence in low-stakes group chats."""
from __future__ import annotations

import os
import re


def silence_eligible(message) -> bool:
    """Never silently drop requests, distress, DMs, attachments, or commands."""
    if os.getenv("NOYA_SILENCE_ENABLED", "true").strip().casefold() not in {"1", "true", "yes", "on"}:
        return False
    if getattr(message, "edit_date", None):
        return False
    chat = getattr(message, "chat", None)
    if str(getattr(chat, "type", "")) not in {"group", "supergroup"}:
        return False
    if any(getattr(message, attr, None) for attr in (
        "photo", "sticker", "animation", "voice", "audio", "video", "video_note", "document",
    )):
        return False
    raw = str(getattr(message, "text", "") or "").strip()
    if not raw or len(raw) > 180 or raw.startswith(("/", "!")) or "?" in raw or "؟" in raw:
        return False
    stripped = re.sub(
        r"^(?:@(?:NuyaRobot|iNuyaRobot)|نویا|noya|nuya)\s*[,،:!.-]*\s*",
        "", raw, flags=re.IGNORECASE,
    ).strip()
    if not stripped or len(stripped) > 150:
        return False
    # No question, task, need for support, or missing information can be skipped.
    task_signals = (
        "میخوام", "می‌خوام", "لطفا", "لطفاً", "بگو", "بده",
        "بفرست", "بساز", "توضیح", "درست", "تحلیل",
        "کمک", "مشکل", "خطا", "ارور", "درد", "ناراحتم",
        "غمگین", "می‌ترسم", "چطور", "چرا", "چجوری",
        "کی ", "کجا", "میشه", "می‌شه", "لینک",
        "متن", "ویس", "عکس", "استیکر", "فرمان",
        "دستور", "انجام", "باید", "کنی", "کن",
    )
    return not any(signal in stripped.casefold() for signal in task_signals)


def silent_token(text: str, *, allowed: bool) -> bool:
    return bool(allowed and isinstance(text, str) and text.strip() == "[[NOYA_SILENT]]")


def silence_instruction() -> str:
    return (
        "در این گروه می‌توانی برای حرف کم‌اهمیت یا گفتگویی که دخالتت بی‌جا است، "
        "به‌جای جواب، سکوت کنی. فقط اگر واقعاً هیچ پاسخ مفیدی لازم نیست، "
        "تمام خروجی را دقیقاً [[NOYA_SILENT]] بنویس. "
        "برای پرسش، کمک‌خواستن، ناراحتی، دستور یا توضیح لازم، هرگز سکوت نکن. "
        "این توکن فقط وقتی از طرف برنامه مجاز شده معتبر است."
    )
