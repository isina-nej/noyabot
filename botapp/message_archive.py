"""Message archival, aligned with real Telegram limitations.

Telegram does NOT deliver a reliable public event when a member or admin
manually deletes a message in an ordinary group. Therefore this module only:

* archives messages the bot itself receives (opt-in per deployment), and
* records deletions the bot itself performs.

It never claims to observe manual deletions.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import re

from django.conf import settings
from django.utils import timezone

from botapp.models import MessageSnapshot

GROUP_CHAT_TYPES = {"group", "supergroup"}
logger = logging.getLogger(__name__)
_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def telegram_message_link(chat, message_id: int) -> str:
    """Build a Telegram permalink only from a verified Telegram chat envelope."""
    chat_id = int(getattr(chat, "id", 0) or 0)
    mid = int(message_id or 0)
    if mid <= 0 or chat_id >= 0 or str(getattr(chat, "type", "")) not in GROUP_CHAT_TYPES:
        return ""
    username = str(getattr(chat, "username", "") or "")
    if _USERNAME.fullmatch(username):
        return f"https://t.me/{username}/{mid}"
    if str(chat_id).startswith("-100") and len(str(chat_id)) > 4:
        return f"https://t.me/c/{str(chat_id)[4:]}/{mid}"
    return ""


def _media_file_id(message) -> tuple[str, str]:
    """Return only Telegram file_id, not media bytes or a temporary file URL."""
    photo = getattr(message, "photo", None) or []
    if photo:
        item = max(photo, key=lambda p: int(getattr(p, "file_size", 0) or 0)
                   or (int(getattr(p, "width", 0) or 0) * int(getattr(p, "height", 0) or 0)))
        return "photo", str(getattr(item, "file_id", "") or "")[:512]
    doc = getattr(message, "document", None)
    if doc and str(getattr(doc, "mime_type", "") or "").startswith("image/"):
        return "document_image", str(getattr(doc, "file_id", "") or "")[:512]
    return "", ""



def archive_enabled() -> bool:
    return bool(getattr(settings, "MESSAGE_ARCHIVE_ENABLED", False))


def _content_type(message) -> str:
    for attr in ("text", "photo", "video", "document", "audio", "voice", "sticker", "animation"):
        if getattr(message, attr, None):
            return "text" if attr == "text" else attr
    if getattr(message, "caption", None):
        return "caption"
    return "other"


def scoped_message_thread_id(message) -> int:
    """Topic ID only when the verified Telegram message represents a topic.

    In non-forum supergroups Telegram can expose a message_thread_id equal
    to the message's own ID (not a forum topic). Never isolate that message
    from the ordinary group history. Real forum topics remain isolated.
    """
    raw = int(getattr(message, "message_thread_id", None) or 0)
    if raw <= 0:
        return 0
    chat = getattr(message, "chat", None)
    if getattr(chat, "is_forum", None) is False:
        return 0
    if getattr(message, "is_topic_message", None) is False:
        return 0
    mid = int(getattr(message, "message_id", 0) or 0)
    if (raw == mid and getattr(chat, "is_forum", None) is not True
            and getattr(message, "is_topic_message", None) is not True):
        return 0
    return raw


def snapshot_payload(message, bot_id: int = 0) -> dict:
    user = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)
    reply = getattr(message, "reply_to_message", None)
    link = telegram_message_link(getattr(message, "chat", None), getattr(message, "message_id", 0))
    kind, file_id = _media_file_id(message)
    metadata = {}
    if user:
        metadata["identity"] = {
            "first_name": str(getattr(user, "first_name", "") or "")[:128],
            "last_name": str(getattr(user, "last_name", "") or "")[:128],
        }
    if link:
        metadata["telegram_link"] = link
    if bot_id and file_id:
        metadata["media_by_bot"] = {str(int(bot_id)): {"file_id": file_id, "kind": kind}}
    return {
        "thread_id": scoped_message_thread_id(message),
        "sender_user_id": int(user.id) if user else None,
        "sender_chat_id": int(sender_chat.id) if sender_chat else None,
        "sender_name": (getattr(user, "full_name", "") if user else "")[:255],
        "sender_username": (getattr(user, "username", "") or "")[:64] if user else "",
        "metadata": metadata,
        "text": (getattr(message, "text", "") or "")[:4096],
        "caption": (getattr(message, "caption", "") or "")[:4096],
        "content_type": _content_type(message),
        "reply_to_message_id": int(reply.message_id) if reply else None,
        "created_at": getattr(message, "date", None),
    }


def archive_message(
    message, bot_id: int = 0, thread_id_override: int | None = None,
) -> MessageSnapshot | None:
    """Upsert a snapshot for a group message. Returns ``None`` when disabled."""
    if not archive_enabled():
        return None
    chat = getattr(message, "chat", None)
    if not chat or str(getattr(chat, "type", "")) not in GROUP_CHAT_TYPES:
        return None
    payload = snapshot_payload(message, bot_id=bot_id)
    # Telegram may omit message_thread_id from the replied-to envelope.
    # A verified same-chat reply supplies its parent's topic in this case.
    if thread_id_override is not None and not getattr(message, "message_thread_id", None):
        payload["thread_id"] = max(0, int(thread_id_override))
    snapshot, created = MessageSnapshot.objects.get_or_create(
        chat_id=int(chat.id),
        message_id=int(message.message_id),
        defaults=payload,
    )
    if not created:
        # Track edits without clobbering deletion metadata.
        changed = False
        for field in ("text", "caption"):
            if getattr(snapshot, field) != payload[field]:
                setattr(snapshot, field, payload[field])
                changed = True
        updated = ["text", "caption", "edited_at"] if changed else []
        # A previously archived non-forum message can have been assigned its
        # own message ID as a spurious topic. The original live update from
        # Telegram lets us correct that without rewriting genuine forum data.
        if (payload["thread_id"] == 0 and int(snapshot.thread_id or 0) == int(message.message_id)
                and (getattr(chat, "is_forum", None) is False
                     or getattr(message, "is_topic_message", None) is False
                     or (getattr(chat, "is_forum", None) is not True
                         and getattr(message, "is_topic_message", None) is not True))):
            snapshot.thread_id = 0
            updated.append("thread_id")
        if (thread_id_override is not None and int(snapshot.thread_id or 0) == 0
                and payload["thread_id"]):
            snapshot.thread_id = payload["thread_id"]
            updated.append("thread_id")
        if not snapshot.reply_to_message_id and payload["reply_to_message_id"]:
            snapshot.reply_to_message_id = payload["reply_to_message_id"]
            updated.append("reply_to_message_id")
        meta = dict(snapshot.metadata or {})
        identity = payload["metadata"].get("identity", {})
        if identity and meta.get("identity") != identity:
            meta["identity"] = identity
        link = payload["metadata"].get("telegram_link")
        if link and meta.get("telegram_link") != link:
            meta["telegram_link"] = link
        by_bot = dict(meta.get("media_by_bot") or {})
        by_bot.update(payload["metadata"].get("media_by_bot") or {})
        if by_bot:
            meta["media_by_bot"] = by_bot
        if meta != snapshot.metadata:
            snapshot.metadata = meta
            updated.append("metadata")
        if changed:
            snapshot.edited_at = timezone.now()
        if updated:
            snapshot.save(update_fields=updated)
    if created and payload["metadata"].get("telegram_link"):
        logger.info("[NOYA-ARCHIVE] link=%s type=%s has_image=%s",
                    payload["metadata"]["telegram_link"], payload["content_type"],
                    bool(payload["metadata"].get("media_by_bot")))
    return snapshot


def mark_deleted_by_bot(chat_id: int, message_ids, reason: str = "", action_id: int | None = None) -> int:
    ids = [int(m) for m in message_ids if m]
    if not ids:
        return 0
    return MessageSnapshot.objects.filter(
        chat_id=int(chat_id),
        message_id__in=ids,
        deleted_by_bot_at__isnull=True,
    ).update(
        deleted_by_bot_at=timezone.now(),
        deletion_reason=(reason or "")[:255],
        deletion_action_id=action_id,
    )


def recent_bot_deleted(chat_id: int, limit: int = 5) -> list[MessageSnapshot]:
    return list(
        MessageSnapshot.objects.filter(
            chat_id=int(chat_id),
            deleted_by_bot_at__isnull=False,
        ).order_by("-deleted_by_bot_at")[: max(limit, 1)]
    )


def recent_archived(chat_id: int, limit: int = 5) -> list[MessageSnapshot]:
    return list(
        MessageSnapshot.objects.filter(chat_id=int(chat_id)).order_by("-archived_at")[: max(limit, 1)]
    )


def purge_old(retention_days: int | None = None) -> int:
    retention_days = retention_days or int(getattr(settings, "MESSAGE_ARCHIVE_RETENTION_DAYS", 30))
    cutoff = timezone.now() - timedelta(days=max(retention_days, 1))
    return MessageSnapshot.objects.filter(archived_at__lt=cutoff).delete()[0]
