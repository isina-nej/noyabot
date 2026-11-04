"""Message archival, aligned with real Telegram limitations.

Telegram does NOT deliver a reliable public event when a member or admin
manually deletes a message in an ordinary group. Therefore this module only:

* archives messages the bot itself receives (opt-in per deployment), and
* records deletions the bot itself performs.

It never claims to observe manual deletions.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from botapp.models import MessageSnapshot

GROUP_CHAT_TYPES = {"group", "supergroup"}


def archive_enabled() -> bool:
    return bool(getattr(settings, "MESSAGE_ARCHIVE_ENABLED", False))


def _content_type(message) -> str:
    for attr in ("text", "photo", "video", "document", "audio", "voice", "sticker", "animation"):
        if getattr(message, attr, None):
            return "text" if attr == "text" else attr
    if getattr(message, "caption", None):
        return "caption"
    return "other"


def snapshot_payload(message) -> dict:
    user = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)
    reply = getattr(message, "reply_to_message", None)
    return {
        "thread_id": int(getattr(message, "message_thread_id", None) or 0),
        "sender_user_id": int(user.id) if user else None,
        "sender_chat_id": int(sender_chat.id) if sender_chat else None,
        "sender_name": (getattr(user, "full_name", "") if user else "")[:255],
        "sender_username": (getattr(user, "username", "") or "")[:64] if user else "",
        "metadata": {
            "identity": {
                "first_name": str(getattr(user, "first_name", "") or "")[:128],
                "last_name": str(getattr(user, "last_name", "") or "")[:128],
            }
        } if user else {},
        "text": (getattr(message, "text", "") or "")[:4096],
        "caption": (getattr(message, "caption", "") or "")[:4096],
        "content_type": _content_type(message),
        "reply_to_message_id": int(reply.message_id) if reply else None,
        "created_at": getattr(message, "date", None),
    }


def archive_message(message) -> MessageSnapshot | None:
    """Upsert a snapshot for a group message. Returns ``None`` when disabled."""
    if not archive_enabled():
        return None
    chat = getattr(message, "chat", None)
    if not chat or str(getattr(chat, "type", "")) not in GROUP_CHAT_TYPES:
        return None
    payload = snapshot_payload(message)
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
        meta = dict(snapshot.metadata or {})
        identity = payload["metadata"].get("identity", {})
        if identity and meta.get("identity") != identity:
            meta["identity"] = identity
            snapshot.metadata = meta
            updated.append("metadata")
        if changed:
            snapshot.edited_at = timezone.now()
        if updated:
            snapshot.save(update_fields=updated)
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
