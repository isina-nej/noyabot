"""Reaction-only Telegram responses for optional, low-stakes social acknowledgements.

The model may choose a reaction only inside this narrow application-controlled
window. User messages cannot enable it by quoting a control token.
"""
from __future__ import annotations

import os
import re

# Telegram's standard reaction emoji, deliberately excluding paid/custom emoji.
REACTION_EMOJIS = frozenset({
    "❤️", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🤯",
    "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏",
    "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤️‍🔥",
    "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔",
    "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈", "😴",
    "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈",
    "😇", "😨", "🤝", "✍", "🤗", "🫡", "🎅",
    "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘",
    "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾",
    "🤷‍♂", "🤷", "🤷‍♀", "😡",
})
# Every standard non-paid emoji documented by Telegram Bot API. Individual
# groups can further restrict which of these are available on their messages.
SUGGESTED_REACTIONS = REACTION_EMOJIS
PREFERRED_REACTIONS = frozenset({"❤️", "👍", "🥰", "👏", "😁", "🎉", "🤩", "🙏", "🤣", "🤝", "👀", "🔥"})
# 💙 is not a standard ReactionTypeEmoji in the Telegram Bot API (Nov 2026).
# Do not silently replace a user's chosen heart color or send an emoji message.
UNSUPPORTED_REQUESTED_REACTIONS = frozenset({"💙"})

_REACTION_PATTERN = re.compile(r"^\[\[NOYA_REACT:([^\[\]\n]{1,12})\]\]$")
_ACK_PREFIX = (
    "مرسی", "ممنون", "دمت گرم", "دستت درد نکنه", "سپاس", "تشکر",
    "ایول", "آفرین", "عالی", "باحال", "خفن", "خوب بود",
    "آره دقیقا", "دقیقا", "حق گفتی", "درست گفتی",
    "خخخ", "هههه", "هاها", "لول", "باشه", "اوکی",
    "قربونت", "فدات", "دوستت دارم", "عشقی", "چه خوب",
    "هورا", "عه", "وااای", "وای چه", "دمت",
)
_TASK_HINTS = (
    "چرا", "چطور", "چجوری", "چی", "کی ", "کجا", "کدوم",
    "میشه", "می‌شه", "میخوام", "می‌خوام", "لطفا", "لطفاً",
    "بگو", "بفرست", "بده", "کن ", "بساز", "درست کن",
    "توضیح", "راهنمایی", "آموزش", "بررسی", "سرچ",
    "کمک", "سؤال", "سوال", "مشکل", "خطا", "ارور",
    "غمگین", "ناراحتم", "افسرده", "مریض", "درد", "می‌ترسم",
)


def _enabled() -> bool:
    return os.getenv("NOYA_REACTION_ENABLED", "true").strip().casefold() in {"1", "true", "yes", "on"}


def explicit_reaction_intent(text: str) -> bool:
    """Only a clear current-message reaction request bypasses social gating."""
    raw = str(text or "").casefold()
    return (
        any(word in raw for word in ("ریاکشن", "ری‌اکشن", "ری اکشن", "reaction", "react"))
        or (
            any(word in raw for word in ("قلب", "ایموجی"))
            and any(word in raw for word in ("بزن", "بذار", "بگذار", "بزار", "بفرست"))
        )
    )


def _social_text(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r"^(?:@(?:NuyaRobot|iNuyaRobot)|نویا|noya|nuya)\s*[,،:!.-]*\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def reaction_eligible(message) -> bool:
    """Permit model choice only for uncomplicated acknowledgements.

    No questions, substantive requests, distress, commands or attached media.
    Short positive acknowledgements can still receive text when the model
    judges conversation would benefit from it.
    """
    if not _enabled() or getattr(message, "edit_date", None):
        return False
    for attr in ("photo", "sticker", "animation", "document", "voice", "audio", "video", "video_note"):
        if getattr(message, attr, None):
            return False
    raw = str(getattr(message, "text", "") or "").strip()
    if not raw or len(raw) > 120 or raw.startswith(("/", "!")):
        return False
    text = _social_text(raw).casefold()
    if not text or "?" in text or "؟" in text:
        return False
    if any(hint in text for hint in _TASK_HINTS):
        return False
    if any(text.startswith(hint) for hint in _ACK_PREFIX):
        return True
    # Explicit pure emoji: allow a reaction, but no arbitrary symbols such as ".".
    emoji_chars = set("❤️💕💖🥰😍😘🤣😂😁👏👍🙏🔥🎉✨👌💯💛🫶😎🤩🥳👀🤝🫡😭")
    significant = [char for char in text if not char.isspace() and char not in "\u200d\ufe0f"]
    return bool(significant) and len(significant) <= 12 and all(char in emoji_chars for char in significant)


def extract_reaction(text: str, *, allowed: bool) -> str | None:
    """Only a whole-message control token from an eligible model response works."""
    if not allowed or not isinstance(text, str):
        return None
    match = _REACTION_PATTERN.fullmatch(text.strip())
    if not match:
        return None
    emoji = match.group(1)
    return emoji if emoji in SUGGESTED_REACTIONS else None


def reaction_instruction() -> str:
    return (
        "این پیام یک واکنش اجتماعی سبک است، نه درخواست اطلاعات یا اقدام. "
        "اگر ادامه‌دادن گفتگو ارزش دارد، با لحن گرم و انسانی و چند جمله طبیعی پاسخ بده؛ "
        "فقط اگر پاسخ متنی واقعاً اضافی است، به‌جایش یکی از ری‌اکشن‌های مجاز را انتخاب کن. "
        "برای ری‌اکشنِ تنها، تمام خروجی می‌تواند دقیقاً [[NOYA_REACT:❤️]] باشد. "
        "نمونه‌های مناسب: ❤️ 👍 🥰 👏 😁 🎉 🤩 🙏 🤣 🤝 👀 🔥؛ "
        "سایر ری‌اکشن‌های استاندارد مجاز نیز قابل انتخاب‌اند. "
        "در این حالت هیچ جمله، توضیح، نقل قول یا علامت دیگری ننویس. "
        "اگر موضوع نیاز به همدلی کلامی، جواب، گفتگوی واقعی یا کمک دارد، حتماً متن طبیعی بنویس؛ "
        "ری‌اکشن جایگزین پرسش و پاسخ نیست."
    )
