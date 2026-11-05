"""Read-only, authenticated access to Noya's opt-in group archive.

Never grants the LLM SQL, server log, filesystem, tokens or other chats.
Telegram Bot API cannot fetch an arbitrary historical message by ID; only
snapshots and file IDs that a running bot previously observed are accessible.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from contextvars import ContextVar
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from botapp.message_archive import archive_enabled, telegram_message_link
from botapp.models import MessageSnapshot, NoyaObservedIdentity

logger = logging.getLogger(__name__)
_SCOPE: ContextVar[dict | None] = ContextVar("noya_group_archive_scope", default=None)
_LINK_MESSAGE = re.compile(r"^[1-9][0-9]{0,14}$")
_TOPIC_TYPES = {"group", "supergroup"}
MAX_ARCHIVE_LOOKUPS = 8


def _trusted_scope() -> dict | None:
    scope = _SCOPE.get()
    if not isinstance(scope, dict):
        return None
    if (scope.get("chat_type") not in _TOPIC_TYPES
            or int(scope.get("chat_id") or 0) >= 0
            or int(scope.get("current_message_id") or 0) <= 0
            or not archive_enabled()
            or not getattr(settings, "NOYA_HISTORY_ENABLED", True)):
        return None
    return scope


def _budget() -> dict | None:
    scope = _trusted_scope()
    if scope is None:
        return None
    scope["queries"] = int(scope.get("queries", 0)) + 1
    return scope if scope["queries"] <= MAX_ARCHIVE_LOOKUPS else None


def _permalink(scope: dict, row: MessageSnapshot) -> str:
    meta = row.metadata if isinstance(row.metadata, dict) else {}
    return str(meta.get("telegram_link") or telegram_message_link(
        scope.get("chat"), row.message_id,
    ))[:200]


def _summarize(row: MessageSnapshot, scope: dict) -> dict:
    meta = row.metadata if isinstance(row.metadata, dict) else {}
    media = meta.get("noya_media") if isinstance(meta.get("noya_media"), dict) else {}
    caption = (row.text or row.caption or "")[:800]
    return {
        "message_id": row.message_id,
        "link": _permalink(scope, row),
        "timestamp": row.created_at.isoformat() if row.created_at else None,
        "user_id": row.sender_user_id,
        "name": row.sender_name[:80],
        "username": row.sender_username[:64],
        "reply_to_message_id": row.reply_to_message_id,
        "kind": row.content_type,
        "text": caption,
        "audio_transcript": str(media.get("transcript") or "")[:650],
        "video_summary": str(media.get("visual_summary") or "")[:400],
        "image_text": str(media.get("image_text") or "")[:1100],
        "image_available": bool(
            (meta.get("media_by_bot") or {}).get(str(scope.get("bot_id", "")))
        ),
    }


def _get_snapshot(scope: dict, mid: int) -> MessageSnapshot | None:
    if mid <= 0 or mid >= int(scope["current_message_id"]):
        return None
    cutoff = timezone.now() - timedelta(
        days=max(1, min(365, int(getattr(settings, "MESSAGE_ARCHIVE_RETENTION_DAYS", 30))))
    )
    return MessageSnapshot.objects.filter(
        chat_id=scope["chat_id"], thread_id=scope["thread_id"],
        message_id=mid, archived_at__gte=cutoff,
        deleted_by_bot_at__isnull=True,
    ).first()


def _resolve_id(scope: dict, reference: str) -> int | None:
    ref = str(reference or "").strip()
    if _LINK_MESSAGE.fullmatch(ref):
        return int(ref)
    try:
        url = urlparse(ref)
    except ValueError:
        return None
    if url.scheme != "https" or url.hostname not in {"t.me", "www.t.me"} or url.port is not None or url.username or url.password:
        return None
    if url.query or url.fragment:
        return None
    segments = url.path.strip("/").split("/")
    chat = scope.get("chat")
    username = str(getattr(chat, "username", "") or "")
    if len(segments) == 2 and username and segments[0].casefold() == username.casefold():
        return int(segments[1]) if _LINK_MESSAGE.fullmatch(segments[1]) else None
    private_id = str(scope["chat_id"])[4:] if str(scope["chat_id"]).startswith("-100") else ""
    if (len(segments) == 3 and segments[0] == "c"
            and segments[1] == private_id and _LINK_MESSAGE.fullmatch(segments[2])):
        return int(segments[2])
    return None


def _search_sync(scope: dict, query: str, before: int, limit: int) -> list[dict]:
    from django.db.models import Q
    cutoff = timezone.now() - timedelta(
        days=max(1, min(365, int(getattr(settings, "MESSAGE_ARCHIVE_RETENTION_DAYS", 30))))
    )
    q = MessageSnapshot.objects.filter(
        chat_id=scope["chat_id"], thread_id=scope["thread_id"],
        message_id__lt=min(int(scope["current_message_id"]), before or int(scope["current_message_id"])),
        archived_at__gte=cutoff,
        deleted_by_bot_at__isnull=True,
    )
    if query:
        q = q.filter(Q(text__icontains=query) | Q(caption__icontains=query)
                     | Q(sender_name__icontains=query))
    return [_summarize(r, scope) for r in q.order_by("-message_id")[:limit]]


async def search_group_messages(query: str = "", before_message_id: int = 0, limit: int = 8) -> str:
    scope = _budget()
    if scope is None:
        return "Error: آرشیو این گروه فعال یا مجاز نیست یا سقف جست‌وجو تکمیل شده."
    if not isinstance(query, str) or len(query) > 100:
        return "Error: عبارت جست‌وجو نامعتبر است."
    try:
        before, count = int(before_message_id), min(max(int(limit), 1), 12)
    except (TypeError, ValueError):
        return "Error: پارامتر جست‌وجو نامعتبر است."
    if before < 0:
        return "Error: شناسه پیام نامعتبر است."
    rows = await sync_to_async(_search_sync, thread_sensitive=True)(
        scope, query.strip(), before, count,
    )
    return json.dumps({"messages": rows, "has_results": bool(rows)}, ensure_ascii=False)


async def get_group_message(reference: str = "") -> str:
    scope = _budget()
    if scope is None:
        return "Error: آرشیو این گروه فعال یا مجاز نیست."
    mid = _resolve_id(scope, reference)
    if mid is None:
        return "Error: لینک/شناسه باید متعلق به همین گروه باشد."
    row = await sync_to_async(_get_snapshot, thread_sensitive=True)(scope, mid)
    if row is None:
        return "این پیام در آرشیو مجاز همین گروه و موضوع پیدا نشد؛ تلگرام امکان واکشی آزاد پیام‌های قدیمی را به ربات نمی‌دهد."
    return json.dumps(_summarize(row, scope), ensure_ascii=False)


def _lookup_nickname_sync(scope: dict, name: str) -> list[dict]:
    from django.db.models import Q
    q = NoyaObservedIdentity.objects.filter(chat_id=scope["chat_id"]).filter(
        Q(nickname__icontains=name) | Q(assigned_nickname__icontains=name)
        | Q(username__icontains=name) | Q(first_name__icontains=name)
        | Q(last_name__icontains=name)
    ).order_by("-updated_at")[:8]
    return [{
        "user_id": row.user_id,
        "username": row.username,
        "name": " ".join(filter(None, [row.first_name, row.last_name])),
        "self_nickname": row.nickname,
        "group_assigned_nickname": row.assigned_nickname,
        "assigned_by_user_id": row.assigned_nickname_by,
    } for row in q]


async def find_group_nickname(name: str = "") -> str:
    scope = _budget()
    if scope is None:
        return "Error: جست‌وجوی هویت فقط در گروه فعلیِ دارای آرشیو مجاز است."
    name = str(name or "").strip().lstrip("@")
    if not 2 <= len(name) <= 64:
        return "Error: اسم یا لقب باید بین دو تا ۶۴ کاراکتر باشد."
    rows = await sync_to_async(_lookup_nickname_sync, thread_sensitive=True)(scope, name)
    return json.dumps({"matches": rows}, ensure_ascii=False)


def _save_image_text(scope: dict, message_id: int, text: str) -> None:
    row = _get_snapshot(scope, message_id)
    if row is None:
        return
    meta = dict(row.metadata or {})
    media = dict(meta.get("noya_media") or {})
    media["image_text"] = text[:1100]
    meta["noya_media"] = media
    row.metadata = meta
    row.save(update_fields=["metadata"])


async def inspect_group_image(reference: str = "") -> str:
    scope = _budget()
    if scope is None:
        return "Error: تصویر فقط در آرشیو مجاز همین گروه قابل بررسی است."
    if int(scope.get("vision_calls") or 0) >= 2:
        return "Error: سقف دو بررسی تصویر در هر درخواست تکمیل شده."
    mid = _resolve_id(scope, reference)
    if mid is None:
        return "Error: لینک/شناسه باید متعلق به همین گروه باشد."
    row = await sync_to_async(_get_snapshot, thread_sensitive=True)(scope, mid)
    if row is None:
        return "پیام در آرشیو این گروه/موضوع نیست. اگر قدیمی است، روی تصویر اصلی ریپلای کن."
    meta = row.metadata if isinstance(row.metadata, dict) else {}
    media = meta.get("noya_media") if isinstance(meta.get("noya_media"), dict) else {}
    if media.get("image_text"):
        return json.dumps({"link": _permalink(scope, row), "visible_text": str(media["image_text"])[:1100]}, ensure_ascii=False)
    info = (meta.get("media_by_bot") or {}).get(str(scope.get("bot_id", ""))) or {}
    file_id = info.get("file_id", "")
    kind = info.get("kind", "")
    if not file_id or kind not in {"photo", "document_image"}:
        return "فایل این عکس در آرشیو همین ربات ثبت نشده؛ تصویر قدیمی فقط از روی لینک قابل دانلود نیست. روی عکس اصلی ریپلای کن."
    bot = scope.get("bot")
    if bot is None:
        return "اتصال تلگرام برای بازیابی عکس موجود نیست."
    scope["vision_calls"] = int(scope.get("vision_calls") or 0) + 1
    try:
        item = await bot.get_file(file_id)
        if int(getattr(item, "file_size", 0) or 0) > 8_000_000:
            return "اندازه فایل عکس برای بررسی بیشتر از حد مجاز است."
        buf = io.BytesIO()
        await bot.download_file(item.file_path, destination=buf)
        raw = buf.getvalue()
        if not raw or len(raw) > 8_000_000:
            return "دانلود فایل تصویر ناموفق یا بیش از حد مجاز بود."
        from botapp.telegram_media import normalize_image_bytes, to_data_url
        mime, normalized = normalize_image_bytes(raw)
        url = (os.getenv("NOYA_API_URL") or "").strip()
        key = (os.getenv("NOYA_API_KEY") or "").strip()
        model = (os.getenv("NOYA_VISION_MODEL") or os.getenv("NOYA_TOOL_MODEL") or os.getenv("NOYA_MODEL") or "").strip()
        if not url or not key or not model:
            return "مدل بینایی برای خواندن عکس قدیمی پیکربندی نشده است."
        request = {
            "model": model,
            "stream": False,
            "max_tokens": 450,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": (
                    "متنِ قابل مشاهده در این عکس تلگرام را دقیق بخوان. "
                    "اگر فارسی است، همان فارسی را بازنویسی کن؛ "
                    "چیزهای نامشخص را حدس نزن. "
                    "پس از متن، در یک جمله تصویر را توصیف کن."
                )},
                {"type": "image_url", "image_url": {"url": to_data_url(mime, normalized)}},
            ]}],
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(url, headers={"Authorization": "Bearer " + key}, json=request)
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"].get("content", "")
        answer = str(answer or "").strip()[:1100]
        if not answer:
            return "مدل بینایی متن قابل اتکایی برنگرداند؛ محتوای تصویر را حدس نزن."
        await sync_to_async(_save_image_text, thread_sensitive=True)(scope, mid, answer)
        return json.dumps({"link": _permalink(scope, row), "visible_text": answer}, ensure_ascii=False)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, OSError):
        logger.info("Archived Telegram photo unavailable chat_scope=group message_id=%s", mid)
        return "دریافت یا تحلیل این تصویر ممکن نشد؛ متن داخل آن تأیید نشده است."
    except Exception as exc:
        logger.warning("Archived photo read failed type=%s", type(exc).__name__)
        return "خواندن تصویر آرشیوشده ناموفق بود؛ متن داخل آن را حدس نزن."
