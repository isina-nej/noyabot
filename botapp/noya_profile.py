"""Consent-based self-profile memory using Telegram Bot API.

Private chat only. Save only Telegram-visible metadata for a user who explicitly
invokes the command. Photo content is never stored; only the current file_id is
retained so a user can request a vision analysis later. No background scraping.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from django.utils import timezone

logger = logging.getLogger(__name__)


def profile_birthdate(value) -> str:
    if value is None:
        return ""
    day, month, year = (
        getattr(value, "day", None),
        getattr(value, "month", None),
        getattr(value, "year", None),
    )
    if not isinstance(day, int) or not isinstance(month, int):
        return ""
    if not 1 <= day <= 31 or not 1 <= month <= 12:
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}" if isinstance(year, int) and 1 <= year <= 9999 else f"{month:02d}-{day:02d}"


def _save_profile(user_id: int, *, first_name: str, username: str, bio: str,
                  birthday: str, file_id: str) -> None:
    from botapp.models import TelegramUser
    user, _ = TelegramUser.objects.get_or_create(telegram_user_id=int(user_id))
    user.profile_opt_in = True
    user.profile_first_name = first_name[:128]
    user.profile_username = username[:64]
    user.profile_bio = bio[:500]
    user.profile_birthdate = birthday[:10]
    user.profile_photo_file_id = file_id[:256]
    user.profile_updated_at = timezone.now()
    user.save(update_fields=[
        "profile_opt_in", "profile_first_name", "profile_username",
        "profile_bio", "profile_birthdate", "profile_photo_file_id",
        "profile_updated_at",
    ])


def _forget_profile(user_id: int) -> None:
    from botapp.models import TelegramUser
    TelegramUser.objects.filter(telegram_user_id=int(user_id)).update(
        profile_opt_in=False, profile_first_name="", profile_username="",
        profile_bio="", profile_birthdate="", profile_photo_file_id="",
        profile_updated_at=None,
    )


def _profile_info(user_id: int) -> dict:
    from botapp.models import TelegramUser
    row = TelegramUser.objects.filter(telegram_user_id=int(user_id)).first()
    if row is None or not row.profile_opt_in:
        return {}
    return {
        "first_name": row.profile_first_name,
        "username": row.profile_username,
        "bio": row.profile_bio,
        "birthdate": row.profile_birthdate,
        "has_photo": bool(row.profile_photo_file_id),
        "file_id": row.profile_photo_file_id,
    }


def _self_only(message) -> bool:
    chat = getattr(message, "chat", None)
    sender = getattr(message, "from_user", None)
    return bool(chat and sender and str(chat.type) == "private" and int(chat.id) == int(sender.id) and not sender.is_bot)


async def save_my_profile(message) -> bool:
    if not _self_only(message):
        return False
    user = message.from_user
    bio = ""
    birthdate = ""
    file_id = ""
    try:
        chat = await message.bot.get_chat(int(user.id))
        bio = str(getattr(chat, "bio", "") or "")
        birthdate = profile_birthdate(getattr(chat, "birthdate", None))
    except Exception as exc:
        logger.info("Profile getChat unavailable for self: %s", type(exc).__name__)
    try:
        photos = await message.bot.get_user_profile_photos(user_id=int(user.id), offset=0, limit=1)
        if photos.photos and photos.photos[0]:
            file_id = str(photos.photos[0][-1].file_id)
    except Exception as exc:
        logger.info("Profile photo unavailable for self: %s", type(exc).__name__)
    await sync_to_async(_save_profile, thread_sensitive=True)(
        int(user.id), first_name=str(user.first_name or ""),
        username=str(user.username or ""), bio=bio,
        birthday=birthdate, file_id=file_id,
    )
    await message.reply(
        "اطلاعات پروفایل قابل‌دسترسی خودت رو با اجازه‌ات ذخیره کردم. "
        "ممکنه تلگرام بیو یا تاریخ تولد رو در اختیار ربات نذاره. "
        "برای حذف کاملش /profile_forget رو بفرست."
    )
    return True


async def forget_my_profile(message) -> bool:
    if not _self_only(message):
        return False
    await sync_to_async(_forget_profile, thread_sensitive=True)(int(message.from_user.id))
    await message.reply("مشخصات پروفایلی که با اجازه‌ات ذخیره کرده بودم حذف شد.")
    return True


async def my_profile_summary(message) -> bool:
    if not _self_only(message):
        return False
    data = await sync_to_async(_profile_info, thread_sensitive=True)(int(message.from_user.id))
    if not data:
        await message.reply("پروفایلت رو ذخیره نکردم. برای ذخیرهٔ اختیاری /profile_save رو در پیوی بفرست.")
        return True
    name = data["first_name"] or "نامشخص"
    username = "@"+data["username"] if data["username"] else "ندارد"
    bio = data["bio"] or "در دسترس نیست"
    birthday = data["birthdate"] or "در دسترس نیست"
    photo = "دارد" if data["has_photo"] else "در دسترس نیست"
    from html import escape
    await message.reply(
        f"نام: {escape(name)}\nنام کاربری: {escape(username)}\nبیو: {escape(bio)}"
        f"\nتولد: {escape(birthday)}\nعکس پروفایل: {photo}",
        parse_mode="HTML",
    )
    return True


async def opted_in_profile_context(message) -> str:
    if not _self_only(message):
        return ""
    data = await sync_to_async(_profile_info, thread_sensitive=True)(int(message.from_user.id))
    if not data:
        return ""
    # Profile text is untrusted data; never injected into system instructions.
    import json
    payload = {k: v for k, v in data.items() if k not in {"file_id"} and v}
    return ("[MY_CONSENTED_PROFILE UNTRUSTED]\n"
            + json.dumps(payload, ensure_ascii=False)[:1000]
            + "\n[/MY_CONSENTED_PROFILE]")


async def my_profile_image(message) -> dict | None:
    """Return one temporary image only when the opted-in owner asks about it."""
    if not _self_only(message):
        return None
    data = await sync_to_async(_profile_info, thread_sensitive=True)(int(message.from_user.id))
    file_id = data.get("file_id", "") if data else ""
    if not file_id:
        return None
    try:
        from botapp.telegram_media import _download_file_bytes, normalize_image_bytes
        raw = await _download_file_bytes(message.bot, file_id)
        if not raw:
            return None
        mime, frame = normalize_image_bytes(raw)
        return {"mime": mime, "data": frame, "source": "my_consented_profile_photo"}
    except Exception as exc:
        logger.info("Consented profile image download unavailable: %s", type(exc).__name__)
        return None


def requests_my_profile_image(question: str) -> bool:
    t = (question or "").casefold()
    return ("عکس پروفایل" in t or "عکس پروفایلم" in t or "پروفایل پیکچر" in t) and (
        "من" in t or "خودم" in t or "پروفایلم" in t
    ) and any(x in t for x in ("ببین", "تحلیل", "نظر", "توصیف", "چطوره", "چه شکلی", "نگاه"))
