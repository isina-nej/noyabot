"""Telegram-visible, per-chat identity and explicitly self-chosen nicknames.

Do not extract identities from other users' claims or from model text. Telegram
user IDs are the authority for attribution; display names are only hints.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from botapp.message_archive import archive_enabled
from botapp.models import NoyaObservedIdentity

_NICKNAME_COMMAND = re.compile(r"^/nickname(?:@\w+)?\s+(.+)$", re.I)
_SELF_NICKNAME = re.compile(
    r"^(?:منو|من رو|من را)\s+(.{2,64}?)\s+(?:صدا\s+کن|خطاب\s+کن)[.!؟\s]*$",
    re.I,
)
_NICKNAME_ALLOWED = re.compile(r"^[\wآ-ی\u200c][\wآ-ی\u200c .-]{1,63}$", re.UNICODE)


def _nickname_from_text(text: str) -> str:
    text = str(text or "").strip()
    m = _NICKNAME_COMMAND.fullmatch(text) or _SELF_NICKNAME.fullmatch(text)
    if not m:
        return ""
    nickname = " ".join(m.group(1).split())[:64]
    return nickname if _NICKNAME_ALLOWED.fullmatch(nickname) else ""


def observe_identity(message) -> bool:
    """Persist only Telegram envelope names in archived groups or an opt-in alias."""
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if user is None or chat is None or getattr(user, "is_bot", False):
        return False
    user_id = int(getattr(user, "id", 0) or 0)
    chat_id = int(getattr(chat, "id", 0) or 0)
    if not user_id or not chat_id:
        return False
    nickname = _nickname_from_text(getattr(message, "text", "") or "")
    is_group = str(getattr(chat, "type", "")) in {"group", "supergroup"}
    if not (nickname or (is_group and archive_enabled())):
        return False

    row, created = NoyaObservedIdentity.objects.get_or_create(
        chat_id=chat_id, user_id=user_id,
    )
    values = {
        "username": str(getattr(user, "username", "") or "")[:64],
        "first_name": str(getattr(user, "first_name", "") or "")[:128],
        "last_name": str(getattr(user, "last_name", "") or "")[:128],
    }
    if nickname:
        values["nickname"] = nickname
    changed = [key for key, val in values.items() if getattr(row, key) != val]
    for key in changed:
        setattr(row, key, values[key])
    if changed or created:
        row.save(update_fields=[*changed, "updated_at"])
    elif row.updated_at < timezone.now() - timedelta(days=7):
        row.save(update_fields=["updated_at"])
    return bool(nickname)


def nickname_context_for_message(message) -> str:
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if user is None or chat is None or getattr(user, "is_bot", False):
        return ""
    row = NoyaObservedIdentity.objects.filter(
        chat_id=int(chat.id), user_id=int(user.id),
    ).first()
    if row is None:
        return ""
    profile = {
        "telegram_user_id": row.user_id,
        "username": row.username,
        "first_name": row.first_name,
        "last_name": row.last_name,
        "self_chosen_nickname": row.nickname,
    }
    return (
        "[SPEAKER_DISPLAY_IDENTITY UNTRUSTED]\n"
        + json.dumps(profile, ensure_ascii=False)
        + "\n[/SPEAKER_DISPLAY_IDENTITY]"
    )


def clear_nickname(chat_id: int, user_id: int) -> bool:
    return bool(NoyaObservedIdentity.objects.filter(
        chat_id=int(chat_id), user_id=int(user_id),
    ).exclude(nickname="").update(nickname=""))


def purge_stale_identities(retention_days: int) -> int:
    """Align observed identity retention with group archive maintenance.

    This also expires self-chosen aliases after inactivity; no permanent
    shadow identity table is created.
    """
    cutoff = timezone.now() - timedelta(days=max(1, int(retention_days)))
    deleted, _ = NoyaObservedIdentity.objects.filter(updated_at__lt=cutoff).delete()
    return deleted


def forget_user_identities(user_id: int) -> int:
    deleted, _ = NoyaObservedIdentity.objects.filter(user_id=int(user_id)).delete()
    return deleted
