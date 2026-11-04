"""Optional asynchronous enrichment of *observed, archived* group voice/video.

No raw media is persisted. Jobs are bounded to avoid starving the chat bot.
Background jobs are best effort; a durable task queue is a future upgrade.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings

from botapp.message_archive import archive_enabled
from botapp.models import MessageSnapshot

logger = logging.getLogger(__name__)
_pending: set[asyncio.Task] = set()
_semaphore = asyncio.Semaphore(1)


def _eligible(message) -> bool:
    chat = getattr(message, "chat", None)
    return bool(
        archive_enabled()
        and chat is not None
        and str(getattr(chat, "type", "")) in {"group", "supergroup"}
        and getattr(message, "message_id", None)
    )


def _stored_media(chat_id: int, message_id: int) -> dict:
    meta = MessageSnapshot.objects.filter(
        chat_id=chat_id, message_id=message_id,
    ).values_list("metadata", flat=True).first() or {}
    if not isinstance(meta, dict):
        return {}
    data = meta.get("noya_media")
    return data if isinstance(data, dict) else {}


def _persist_media(chat_id: int, message_id: int, transcript: str, visual: str) -> bool:
    row = MessageSnapshot.objects.filter(
        chat_id=chat_id, message_id=message_id, deleted_by_bot_at__isnull=True,
    ).first()
    if row is None:
        return False
    meta = dict(row.metadata or {})
    stored = dict(meta.get("noya_media") or {})
    if transcript:
        stored["transcript"] = str(transcript).strip()[:1600]
    if visual:
        stored["visual_summary"] = str(visual).strip()[:750]
    if not stored:
        return False
    meta["noya_media"] = stored
    row.metadata = meta
    row.save(update_fields=["metadata"])
    return True


async def save_prepared_media(message, prepared, *, visual: str = "") -> None:
    """Reuse media already processed by an addressed Noya turn."""
    if not _eligible(message):
        return
    from botapp.media_intake import _attachment
    attached = _attachment(message)
    if not attached or attached[0] not in {"audio", "video"}:
        # A quoted voice must not be saved as if the new reply contained audio.
        return
    await sync_to_async(_persist_media, thread_sensitive=True)(
        int(message.chat.id), int(message.message_id),
        str(getattr(prepared, "transcript", "") or ""), visual,
    )


async def _describe_video_frame(images) -> str:
    if not getattr(settings, "NOYA_ARCHIVE_VIDEO_VISION_ENABLED", False) or not images:
        return ""
    from botapp.telegram_media import images_to_data_urls

    urls = images_to_data_urls(images[:3])
    if not urls:
        return ""
    model = (os.getenv("NOYA_VISION_MODEL") or os.getenv("NOYA_MODEL") or "").strip()
    url = (os.getenv("NOYA_API_URL") or "").strip()
    key = (os.getenv("NOYA_API_KEY") or "").strip()
    if not model or not url or not key:
        return ""
    content = [{
        "type": "text",
        "text": (
            "این‌ها فقط فریم‌های نمونه از یک ویدیوی گروه تلگرام هستند. "
            "در دو جمله فقط چیزهای قابل مشاهده و موضوع احتمالی تصویر را بگو؛ "
            "حرکت‌ها، هویت افراد و رخدادهای خارج از فریم را حدس نزن. "
            "این توصیف برای زمینه پاسخ‌های بعدی ذخیره می‌شود."
        ),
    }]
    content.extend({"type": "image_url", "image_url": {"url": image}} for image in urls)
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                url,
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": model,
                    "stream": False,
                    "max_tokens": 180,
                    "messages": [{"role": "user", "content": content}],
                },
            )
            response.raise_for_status()
            answer = response.json()["choices"][0]["message"].get("content", "")
            return str(answer or "").strip()[:750]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        logger.info("Archived video visual summary unavailable", exc_info=False)
        return ""


async def _enrich(bot, message) -> None:
    from botapp.media_intake import _attachment, prepare_media_for_noya

    try:
        async with _semaphore:
            if not _eligible(message):
                return
            chat_id, message_id = int(message.chat.id), int(message.message_id)
            if await sync_to_async(_stored_media, thread_sensitive=True)(chat_id, message_id):
                return
            found = _attachment(message)
            if not found or found[0] not in {"audio", "video"}:
                return
            prepared = await asyncio.wait_for(
                prepare_media_for_noya(bot, message, include_reply=False),
                timeout=120,
            )
            visual = await _describe_video_frame(prepared.images) if found[0] == "video" else ""
            await save_prepared_media(message, prepared, visual=visual)
    except (asyncio.TimeoutError, RuntimeError, ValueError, OSError):
        logger.info("Archived media processing skipped/failed chat=%s message=%s",
                    getattr(getattr(message, "chat", None), "id", 0),
                    getattr(message, "message_id", 0))
    except Exception:
        logger.warning("Archived media enrichment unavailable", exc_info=True)


def schedule_media_enrichment(bot, message) -> bool:
    """Never wait for STT/vision in the outer Telegram middleware."""
    if not getattr(settings, "NOYA_ARCHIVE_MEDIA_ENRICHMENT_ENABLED", False):
        return False
    if not _eligible(message) or bot is None or len(_pending) >= 5:
        return False
    from botapp.media_intake import _attachment
    found = _attachment(message)
    if not found or found[0] not in {"audio", "video"}:
        return False

    task = asyncio.create_task(_enrich(bot, message), name="noya-archived-media")
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return True
