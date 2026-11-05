"""Bounded, same-chat OCR/vision of archived stickers, GIFs and video notes.

Only Telegram file IDs observed by the current authenticated bot are used.
Raw media/frames and API keys are never persisted, exposed to the model or
logged. The existing sampled-frame renderer and speech transcription provider
are reused rather than creating a second unbounded media pipeline.
"""
from __future__ import annotations

import io
import json
import logging
import os

import httpx
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)
MAX_MEDIA_BYTES = 20_000_000
MAX_FRAMES = 3
SUPPORTED = {
    "sticker", "animation", "video_note", "video",
    "document_animation", "document_image",
}


async def _sample_media(raw: bytes, kind: str, info: dict) -> list[tuple[str, bytes]]:
    """Return at most three representative stills from a bounded Telegram file."""
    from botapp.animated_media import sample_animation_frames
    from botapp.telegram_media import normalize_image_bytes
    from botapp.media_intake import _ffmpeg

    if kind in {"video_note", "video"}:
        try:
            return (await sample_animation_frames(raw, kind="video"))[:MAX_FRAMES]
        except (RuntimeError, ValueError, OSError):
            # Keep one genuine frame if the animation worker is unavailable.
            try:
                frame = await _ffmpeg(raw, frame=True)
                return [normalize_image_bytes(frame)]
            except (RuntimeError, ValueError, OSError):
                return []

    if kind == "sticker":
        animated = info.get("is_animated") is True or raw.startswith(b"\x1f\x8b")
        moving_video = info.get("is_video") is True or raw.startswith(b"\x1a\x45\xdf\xa3")
        if animated or moving_video:
            try:
                return (await sample_animation_frames(
                    raw, kind="tgs" if animated else "video",
                ))[:MAX_FRAMES]
            except (RuntimeError, ValueError, OSError):
                return []

    if (kind in {"animation", "document_animation", "sticker"}
            or raw.startswith((b"GIF87a", b"GIF89a"))):
        mime = str(info.get("mime_type") or "").lower()
        name = str(info.get("file_name") or "").lower()
        fmt = "image" if (
            raw.startswith((b"GIF87a", b"GIF89a"))
            or mime == "image/gif" or name.endswith((".gif", ".apng"))
            or (kind == "sticker" and not raw.startswith(b"\x00\x00"))
        ) else "video"
        try:
            return (await sample_animation_frames(raw, kind=fmt))[:MAX_FRAMES]
        except (RuntimeError, ValueError, OSError):
            if kind != "sticker":
                return []
    try:
        return [normalize_image_bytes(raw)]
    except (RuntimeError, ValueError, OSError):
        return []


async def _voice_transcript(raw: bytes) -> tuple[str, str]:
    """Extract actual speech from a video message; silence is not fabricated."""
    from botapp.media_intake import _ffmpeg, transcribe_audio
    try:
        audio = await _ffmpeg(raw, frame=False)
        text = await transcribe_audio(audio, suffix=".mp3", mime="audio/mpeg")
        return str(text or "").strip()[:1600], ""
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        logger.info("Archived video-note STT unavailable type=%s", type(exc).__name__)
        return "", "صدای این ویدیو قابل تبدیل به متن نبود یا گفتاری نداشت."


async def _read_visible_text(frames: list[tuple[str, bytes]], kind: str) -> str:
    """Use configured multimodal provider for real frame OCR and scene content."""
    from botapp.telegram_media import to_data_url

    url = os.getenv("NOYA_API_URL", "").strip()
    key = os.getenv("NOYA_API_KEY", "").strip()
    model = (os.getenv("NOYA_VISION_MODEL")
             or os.getenv("NOYA_TOOL_MODEL")
             or os.getenv("NOYA_MODEL") or "").strip()
    if not url or not key or not model or not frames:
        return ""

    if kind in {"video_note", "video"}:
        instructions = (
            "این‌ها چند فریم نمونهٔ زمانی از یک ویدیو‌مسج/ویدیو هستند، نه کل ویدیو. "
            "همه متن‌های قابل‌خواندن در این فریم‌ها را به زبان اصلی عیناً بازنویسی کن. "
            "سپس آنچه واقعاً دیده می‌شود را خلاصه کن. "
            "رفتار، حرکت یا کلمات ناموجود در فریم‌ها را حدس نزن."
        )
    else:
        instructions = (
            "این تصاویر نمونه‌های زمانی یک GIF یا استیکر تلگرام‌اند. "
            "متن نوشته‌شده روی هر فریم را به زبان اصلی، به‌ترتیب و بدون حدس بازنویسی کن؛ "
            "اگر متنِ قابل مشاهده نیست، صریحاً بگو. "
            "سپس ظاهر، حس و تغییراتِ دیده‌شده بین فریم‌ها را کوتاه توصیف کن. "
            "این‌ها فریم نمونه‌اند نه مشاهدهٔ تمام انیمیشن."
        )
    content: list[dict] = [{"type": "text", "text": instructions}]
    for idx, (mime, data) in enumerate(frames[:MAX_FRAMES], start=1):
        content.append({"type": "text", "text": f"فریم نمونه {idx} از {len(frames[:MAX_FRAMES])}"})
        content.append({"type": "image_url", "image_url": {"url": to_data_url(mime, data)}})
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                url,
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": model, "stream": False, "max_tokens": 700,
                    "messages": [{"role": "user", "content": content}],
                },
            )
            response.raise_for_status()
            result = response.json()["choices"][0]["message"].get("content", "")
        if isinstance(result, list):
            result = " ".join(
                str(item.get("text") or "") for item in result if isinstance(item, dict)
            )
        return str(result or "").strip()[:1800]
    except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError):
        logger.info("Archived animated media vision provider unavailable")
        return ""


def _store_reading(scope: dict, message_id: int, visual: str, transcript: str) -> None:
    from botapp.noya_archive_access import _get_snapshot
    row = _get_snapshot(scope, message_id)
    if row is None:
        return
    metadata = dict(row.metadata or {})
    media = dict(metadata.get("noya_media") or {})
    if visual:
        media["image_text"] = visual[:1800]
    if transcript:
        media["transcript"] = transcript[:1600]
    if not visual and not transcript:
        return
    metadata["noya_media"] = media
    row.metadata = metadata
    row.save(update_fields=["metadata"])


async def inspect_group_media(reference: str = "") -> str:
    """Inspect an archived animation or video only within current group/topic."""
    from botapp.noya_archive_access import (
        _budget, _resolve_id, _get_snapshot, _permalink, inspect_group_image,
    )
    scope = _budget()
    if scope is None:
        return "Error: آرشیو این گروه فعال یا مجاز نیست یا سقف جست‌وجو تمام شده."
    mid = _resolve_id(scope, reference)
    if mid is None:
        return "Error: لینک یا شناسه باید متعلق به همین گروه باشد."
    row = await sync_to_async(_get_snapshot, thread_sensitive=True)(scope, mid)
    if row is None:
        return "رسانه در آرشیو مجاز این گروه و موضوع ثبت نشده است."
    if row.content_type == "photo":
        # Preserve the existing photo OCR path and its regression coverage.
        return await inspect_group_image(reference)
    meta = row.metadata if isinstance(row.metadata, dict) else {}
    media = meta.get("noya_media") if isinstance(meta.get("noya_media"), dict) else {}
    visual = str(media.get("image_text") or media.get("visual_summary") or "")[:1800]
    transcript = str(media.get("transcript") or "")[:1600]
    kind = row.content_type
    if kind not in {"sticker", "animation", "video_note", "video", "document"}:
        return "این پیام ویدیو‌مسج، GIF یا استیکر قابل بررسی نیست."
    if visual:
        return json.dumps({
            "link": _permalink(scope, row), "kind": kind,
            "visible_text": visual, "speech_transcript": transcript,
            "cached": True, "sampling": "previously analyzed frames",
        }, ensure_ascii=False)
    info = (meta.get("media_by_bot") or {}).get(str(scope.get("bot_id") or "")) or {}
    ref_kind = str(info.get("kind") or "")
    if not isinstance(info, dict) or ref_kind not in SUPPORTED or not info.get("file_id"):
        return "فایل این رسانه برای همین ربات ثبت نشده؛ تنها با لینک نمی‌توان GIF/ویدیوی قدیمی را دانلود کرد."
    if kind != "document" and ref_kind != kind:
        return "نوع رکورد رسانه با فایل ثبت‌شده مطابقت ندارد."
    if kind == "document" and ref_kind not in {"document_animation", "document_image"}:
        return "فقط GIF/تصویر ارسالی به شکل فایل قابل بررسی است."
    bot = scope.get("bot")
    if bot is None:
        return "دسترسی ربات به دانلود رسانه در این درخواست برقرار نیست."
    if int(scope.get("vision_calls") or 0) >= 2:
        return "Error: سقف دو تحلیل رسانه در هر درخواست تکمیل شده است."
    scope["vision_calls"] = int(scope.get("vision_calls") or 0) + 1
    try:
        remote = await bot.get_file(info["file_id"])
        if int(getattr(remote, "file_size", 0) or 0) > MAX_MEDIA_BYTES:
            return "حجم رسانه بیش از سقف مجاز پردازش است."
        buf = io.BytesIO()
        await bot.download_file(remote.file_path, destination=buf)
        raw = buf.getvalue()
        if not raw or len(raw) > MAX_MEDIA_BYTES:
            return "فایل رسانه ناقص یا بیش از سقف مجاز است."
        frames = await _sample_media(raw, ref_kind, info)
        speech = ""
        warning = ""
        if ref_kind in {"video_note", "video"}:
            speech, warning = await _voice_transcript(raw)
        if not frames and not speech:
            return "رندر فریم و خواندن صوت این رسانه ممکن نشد؛ متنی را حدس نزن."
        visual = await _read_visible_text(frames, ref_kind) if frames else ""
        if visual or speech:
            await sync_to_async(_store_reading, thread_sensitive=True)(
                scope, mid, visual, speech,
            )
        result = {
            "link": _permalink(scope, row), "kind": ref_kind,
            "sampled_frames": len(frames), "visible_text": visual,
            "speech_transcript": speech, "cached": False,
            "sampling": "حداکثر ۳ فریم نمونه؛ مشاهدهٔ پیوستهٔ کل انیمیشن نیست.",
        }
        if warning:
            result["audio_warning"] = warning
        if frames and not visual:
            result["vision_warning"] = "مدل Vision نتیجهٔ قابل‌اعتماد برنگرداند؛ متن تصویر تأیید نشده."
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        # Never log raw Telegram file_id, media bytes, provider credentials or
        # arbitrary exception messages that can contain signed URLs.
        logger.warning("Archived media reading unavailable type=%s", type(exc).__name__)
        return "دانلود یا بررسی این رسانه ناموفق بود؛ متن داخل آن تأیید نشده است."
