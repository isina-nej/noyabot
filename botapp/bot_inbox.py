"""Opt-in per-bot inbox archive fed by the existing aiogram dispatcher.

No second getUpdates/webhook consumer is started. The source of truth is the
already authenticated Telegram Update, not web-scraped content.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from botapp.models import BotInboxMessage


def enabled() -> bool:
    return bool(getattr(settings, "BOT_INBOX_ENABLED", False))


def get_content_type(message) -> str:
    for attr in ("photo", "sticker", "animation", "video", "voice", "audio",
                 "document", "video_note", "text"):
        if getattr(message, attr, None):
            return attr
    return "caption" if getattr(message, "caption", None) else "other"


def _file_id(message, content_type: str) -> str:
    media = getattr(message, content_type, None)
    if content_type == "photo" and media:
        media = media[-1]
    return (str(getattr(media, "file_id", "") or ""))[:512]


def payload_from_message(message, bot_id: int) -> dict:
    sender = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)
    chat = message.chat
    outgoing = bool(sender is not None and int(sender.id) == int(bot_id))
    chat_type = str(getattr(chat, "type", "private"))
    title = (str(getattr(chat, "title", "") or "") if chat_type != "private" else
             str(getattr(sender, "full_name", "") or getattr(chat, "full_name", "") or ""))
    reply = getattr(message, "reply_to_message", None)
    kind = get_content_type(message)
    from botapp.message_archive import scoped_message_thread_id
    return {
        "chat_type": chat_type[:32],
        "chat_title": title[:255],
        "thread_id": scoped_message_thread_id(message),
        "sender_user_id": int(sender.id) if sender is not None else None,
        "sender_name": str(getattr(sender, "full_name", "") or
                           getattr(sender_chat, "title", "") or "")[:255],
        "sender_username": str(getattr(sender, "username", "") or "")[:64],
        "is_outgoing": outgoing,
        "text": str(getattr(message, "text", "") or "")[:4096],
        "caption": str(getattr(message, "caption", "") or "")[:4096],
        "content_type": kind,
        "media_file_id": _file_id(message, kind),
        "reply_to_message_id": int(reply.message_id) if reply is not None else None,
        "message_date": getattr(message, "date", None),
    }


@transaction.atomic
def archive_message(message, bot_id: int) -> BotInboxMessage | None:
    if not enabled() or not getattr(message, "chat", None):
        return None
    message_id = getattr(message, "message_id", None)
    if message_id is None or not bot_id:
        return None
    data = payload_from_message(message, bot_id)
    item, created = BotInboxMessage.objects.get_or_create(
        bot_id=int(bot_id), chat_id=int(message.chat.id), message_id=int(message_id),
        defaults=data,
    )
    if not created:
        changed = []
        for field in ("text", "caption", "content_type", "media_file_id"):
            if getattr(item, field) != data[field]:
                setattr(item, field, data[field])
                changed.append(field)
        if changed:
            item.edited_at = timezone.now()
            item.save(update_fields=[*changed, "edited_at"])
    return item


def archive_manual_send(*, bot_id: int, chat_id: int, message, text: str) -> BotInboxMessage:
    # Telegram confirmed delivery. The model and API never guess a message id.
    previous = BotInboxMessage.objects.filter(
        bot_id=bot_id, chat_id=chat_id,
    ).order_by("-message_id").first()
    title = previous.chat_title if previous else str(chat_id)
    return BotInboxMessage.objects.update_or_create(
        bot_id=int(bot_id), chat_id=int(chat_id), message_id=int(message.message_id),
        defaults={
            "chat_type": previous.chat_type if previous else "private",
            "chat_title": title,
            "sender_user_id": int(bot_id),
            "sender_name": "NoyaBot",
            "is_outgoing": True,
            "text": text[:4096],
            "content_type": "text",
            "message_date": message.date,
        },
    )[0]


def purge_expired(*, days: int | None = None) -> int:
    ttl = days if days is not None else getattr(settings, "BOT_INBOX_RETENTION_DAYS", 30)
    return BotInboxMessage.objects.filter(
        archived_at__lt=timezone.now() - timedelta(days=max(1, int(ttl))),
    ).delete()[0]
