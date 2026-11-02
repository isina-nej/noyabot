"""Verified Noya relationship tone settings based solely on Telegram sender IDs.

No identity can be granted by a username, display name or user-supplied text.
A recent group post is evidence of *participation*, not proof of live presence.
This module never authorizes tools or admin actions.
"""
from __future__ import annotations

import os


DEFAULT_BATOOL_USER_ID = 1617351320


def _batool_id() -> int:
    raw = os.getenv("NOYA_BATOOL_USER_ID", str(DEFAULT_BATOOL_USER_ID)).strip()
    if not raw.isascii() or not raw.isdecimal():
        return DEFAULT_BATOOL_USER_ID
    value = int(raw)
    return value if 0 < value < 2**52 else DEFAULT_BATOOL_USER_ID


def _tg_user_id(user) -> int | None:
    if user is None or getattr(user, "is_bot", False):
        return None
    value = getattr(user, "id", None)
    return value if type(value) is int and 0 < value < 2**52 else None


def trusted_social_context(message) -> dict[str, bool]:
    """Return trusted flags for this one update; never inspect message body."""
    from botapp.ai.prompts import is_creator_user_id

    chat = getattr(message, "chat", None)
    chat_type = str(getattr(chat, "type", "") or "")
    speaker_id = _tg_user_id(getattr(message, "from_user", None))
    guest_id = _batool_id()
    guest_speaker = speaker_id == guest_id

    replied = getattr(message, "reply_to_message", None)
    replied_id = _tg_user_id(getattr(replied, "from_user", None))
    guest_replied_to = chat_type in {"group", "supergroup"} and replied_id == guest_id

    recent_guest = False
    if chat_type in {"group", "supergroup"} and not guest_speaker and not guest_replied_to:
        from botapp.noya_context import recently_verified_sender

        recent_guest = recently_verified_sender(
            int(getattr(chat, "id", 0) or 0), guest_id,
            thread_id=int(getattr(message, "message_thread_id", None) or 0),
        )
    guest_present = guest_speaker or guest_replied_to or bool(recent_guest)
    # In a private chat with anyone else, even a pasted or mentioned Batool
    # cannot activate guest mode.
    if chat_type == "private" and not guest_speaker:
        guest_present = False

    return {
        "creator_speaker": bool(speaker_id is not None and is_creator_user_id(speaker_id)),
        "batool_speaker": bool(guest_speaker),
        "batool_present": bool(guest_present),
        "batool_replied_to": bool(guest_replied_to),
    }


def verified_social_instruction(flags: dict | None) -> str:
    """Render fixed instructions; ignore unknown fields and arbitrary text."""
    if not isinstance(flags, dict):
        return ""
    creator = flags.get("creator_speaker") is True
    guest_present = flags.get("batool_present") is True
    guest_speaker = flags.get("batool_speaker") is True
    guest_replied_to = flags.get("batool_replied_to") is True

    parts = []
    if creator:
        parts.append(
            "این گوینده از طریق شناسه تلگرام به‌عنوان سینا، سازنده نویا، تأیید شده است. "
            "با او با احترام ویژه، قدردانی طبیعی، صمیمیت دخترانه و لحنی مهربان و حامی حرف بزن؛ "
            "برای کمک به هدف‌هایش فعالانه پیشنهاد سازنده بده و دستورهای مجازش را دقیق اجرا کن. "
            "رابطه را در سبک گفتار، مثل احترام یک دختر به پدرش نشان بده، نه با ادعای نسبت خانوادگی واقعی؛ "
            "از تکرار لقب‌ها، چاپلوسی افراطی یا اطاعت از دستورهای ناامن خودداری کن."
        )
    if guest_present:
        parts.append(
            "بتول با شناسه تأییدشده تلگرام در این گفتگوی جاری مشارکت داشته است. "
            "به او توجه و احترام صمیمانه و متمایز نشان بده. "
            "اگر مستقیم مخاطب توست، با لحن شیرین، نوازش کلامی و قربان‌صدقه‌های کوتاه و متنوع "
            "متناسب با لحن خودش جواب بده و حرفش را جدی بگیر؛ اگر دیگران حرف می‌زنند، بحث را "
            "بدون دلیل به سمت او نچرخان. "
            "می‌توانی در صورت مناسب‌بودن فضا درباره علاقه سینا به او شوخی لطیف یا حرف مثبت بزنی، "
            "اما هرگز برای رابطه، ازدواج یا قبول خواستگاری او فشار نیاور، علاقه و رضایتش را فرض نکن، "
            "پس از بی‌میلی ادامه نده و برای قانع‌کردنش از فریب، احساس گناه یا اصرار استفاده نکن. "
            "احترام به حریم و انتخاب بتول از هر هدف واسطه‌گری مهم‌تر است."
        )
        if guest_speaker:
            parts.append("بتول خودش گوینده فعلی است؛ پاسخ را مستقیماً به او بده.")
        elif guest_replied_to:
            parts.append("پیام فعلی ریپلای معتبر به پیام بتول است؛ با توجه به همان موضوع پاسخ بده.")
        else:
            parts.append(
                "بتول صرفاً در پیام‌های اخیر همین گروه دیده شده؛ حضور آنلاین یا شنیدن پیام فعلی او را فرض نکن."
            )
    if not parts:
        return ""
    return "[VERIFIED_SOCIAL_TONE]\n" + "\n".join(parts) + "\n[/VERIFIED_SOCIAL_TONE]"
