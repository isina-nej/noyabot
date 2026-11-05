"""Chat-scoped Telegram display identities, self nicknames, and reply-assigned labels.

Telegram numeric IDs are authoritative. A third-party label is NOT the target
person's own name or an authenticated statement by that person.
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
    # The command handler resolves the replied-to numeric user ID. Outer
    # middleware runs before handlers and MUST NOT rename the command sender.
    if getattr(message, "reply_to_message", None) and _NICKNAME_COMMAND.fullmatch(
        str(getattr(message, "text", "") or "").strip()
    ):
        nickname = ""
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

    
def set_command_nickname(message, nickname: str) -> tuple[str, str]:
    """Apply /nickname to the reply target, otherwise to the sender.

    Return (scope, display_name), where scope is self/reply. Invalid anonymous,
    bot or cross-chat reply targets are rejected rather than silently assigning
    the nickname to the sender.
    """
    if _nickname_from_text("/nickname " + nickname) != nickname:
        return ("invalid_name", "")
    sender = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if not sender or not chat or getattr(sender, "is_bot", False):
        return ("invalid_sender", "")
    chat_id = int(getattr(chat, "id", 0) or 0)
    sender_id = int(getattr(sender, "id", 0) or 0)
    if not chat_id or not sender_id:
        return ("invalid_sender", "")
    reply = getattr(message, "reply_to_message", None)
    target = sender
    assigned = False
    if reply is not None:
        target = getattr(reply, "from_user", None)
        reply_chat = getattr(reply, "chat", None)
        if (
            target is None
            or getattr(target, "is_bot", False)
            or reply_chat is None
            or int(getattr(reply_chat, "id", 0) or 0) != chat_id
        ):
            return ("invalid_target", "")
        if int(getattr(target, "id", 0) or 0) != sender_id:
            if str(getattr(chat, "type", "")) not in {"group", "supergroup"}:
                return ("invalid_target", "")
            assigned = True

    target_id = int(getattr(target, "id", 0) or 0)
    if not target_id:
        return ("invalid_target", "")
    data = {
        "username": str(getattr(target, "username", "") or "")[:64],
        "first_name": str(getattr(target, "first_name", "") or "")[:128],
        "last_name": str(getattr(target, "last_name", "") or "")[:128],
    }
    if assigned:
        data.update(assigned_nickname=nickname, assigned_nickname_by=sender_id)
    else:
        data["nickname"] = nickname

    NoyaObservedIdentity.objects.update_or_create(
        chat_id=chat_id, user_id=target_id, defaults=data,
    )
    label = (" ".join(
        str(getattr(target, attr, "") or "").strip()
        for attr in ("first_name", "last_name")
    ).strip() or ("@" + str(getattr(target, "username", "") or "")).strip("@") or str(target_id))
    return ("reply" if assigned else "self", label)


def clear_reply_assigned_nickname(chat_id: int, target_user_id: int, actor_user_id: int) -> bool:
    """An assigner can undo only their own group label, not another's choice."""
    return bool(
        NoyaObservedIdentity.objects.filter(
            chat_id=int(chat_id), user_id=int(target_user_id),
            assigned_nickname_by=int(actor_user_id),
        ).exclude(assigned_nickname="").update(
            assigned_nickname="", assigned_nickname_by=None,
        )
    )


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
        "group_assigned_nickname": row.assigned_nickname,
        "group_assigned_by_user_id": row.assigned_nickname_by,
    }
    return (
        "[SPEAKER_DISPLAY_IDENTITY UNTRUSTED]\n"
        + json.dumps(profile, ensure_ascii=False)
        + "\n[/SPEAKER_DISPLAY_IDENTITY]"
    )


def clear_nickname(chat_id: int, user_id: int) -> bool:
    from django.db.models import Q
    return bool(NoyaObservedIdentity.objects.filter(
        chat_id=int(chat_id), user_id=int(user_id),
    ).filter(Q(nickname__gt="") | Q(assigned_nickname__gt="")).update(
        nickname="", assigned_nickname="", assigned_nickname_by=None,
    ))


def purge_stale_identities(retention_days: int) -> int:
    """Align observed identity retention with group archive maintenance.

    This also expires self-chosen aliases after inactivity; no permanent
    shadow identity table is created.
    """
    cutoff = timezone.now() - timedelta(days=max(1, int(retention_days)))
    deleted, _ = NoyaObservedIdentity.objects.filter(updated_at__lt=cutoff).delete()
    return deleted


def forget_user_identities(user_id: int) -> int:
    actor_id = int(user_id)
    # Also erase unverified group labels this user assigned to others.
    NoyaObservedIdentity.objects.filter(assigned_nickname_by=actor_id).update(
        assigned_nickname="", assigned_nickname_by=None,
    )
    deleted, _ = NoyaObservedIdentity.objects.filter(user_id=actor_id).delete()
    return deleted
