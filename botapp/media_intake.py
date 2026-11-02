"""Bounded Telegram media intake for Noya: vision, speech, video and documents.

Media is request-local; never write raw attachments, transcripts or tokens to logs.
Transcription is an optional OpenAI-compatible /audio/transcriptions service.
An unavailable provider yields an explicit warning, never invented speech.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from botapp.telegram_media import MAX_IMAGES, collect_noya_images, normalize_image_bytes

logger = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".ogg", ".oga", ".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac", ".wma", ".opus", ".webm"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".mpeg", ".mpg", ".3gp"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".log", ".py", ".yaml", ".yml"}
DOCUMENT_SUFFIXES = TEXT_SUFFIXES | {".pdf", ".docx"}


def _limit(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _max_bytes() -> int:
    # Telegram's cloud Bot API normally limits getFile downloads to 20 MiB.
    return _limit("NOYA_MEDIA_MAX_BYTES", 20_000_000, 1024, 20_000_000)


def _max_seconds() -> int:
    return _limit("NOYA_MEDIA_MAX_SECONDS", 180, 5, 600)


@dataclass
class PreparedMedia:
    images: list[dict] = field(default_factory=list)
    context: str = ""
    transcript: str = ""
    warnings: list[str] = field(default_factory=list)
    has_media: bool = False
    has_audio: bool = False


def _attachment(message):
    """Get a single supported attachment; voice/audio/video first, then documents."""
    for key, kind in (("voice", "audio"), ("audio", "audio"), ("video", "video"),
                      ("video_note", "video"), ("document", "document")):
        obj = getattr(message, key, None)
        if obj is None:
            continue
        name = str(getattr(obj, "file_name", "") or "")
        suffix = Path(name.lower()).suffix
        mime = str(getattr(obj, "mime_type", "") or "").lower().split(";", 1)[0]
        if not suffix:
            suffix = {
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                "audio/wav": ".wav", "audio/webm": ".webm",
                "video/mp4": ".mp4", "video/webm": ".webm",
                "application/pdf": ".pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            }.get(mime, "")
        if kind == "document":
            if mime.startswith("image/"):
                return None  # existing vision path handles image documents
            if mime.startswith("audio/") or suffix in AUDIO_SUFFIXES:
                kind = "audio"
            elif mime.startswith("video/") or suffix in VIDEO_SUFFIXES:
                kind = "video"
            elif suffix in DOCUMENT_SUFFIXES or mime.startswith("text/"):
                kind = "text"
            else:
                kind = "unsupported"
        return kind, obj, suffix, mime
    return None


async def _download(bot, obj) -> bytes:
    size = int(getattr(obj, "file_size", 0) or 0)
    if size > _max_bytes():
        raise ValueError("حجم فایل از سقف مجاز دریافت تلگرام بیشتر است.")
    file = await bot.get_file(obj.file_id)
    remote_size = int(getattr(file, "file_size", 0) or 0)
    if remote_size > _max_bytes():
        raise ValueError("حجم فایل از سقف مجاز دریافت تلگرام بیشتر است.")
    buffer = io.BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    raw = buffer.getvalue()
    if len(raw) > _max_bytes():
        raise ValueError("حجم فایل از سقف مجاز بیشتر است.")
    return raw


async def _ffmpeg(raw: bytes, *, frame: bool = False) -> bytes:
    """Run ffmpeg without a shell, cap processing duration and kill on timeout."""
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
    if frame:
        command += ["-frames:v", "1", "-vf", "scale=640:-2",
                    "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    else:
        command += ["-t", str(_max_seconds()), "-vn", "-ac", "1", "-ar", "16000",
                    "-b:a", "48k", "-f", "mp3", "pipe:1"]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("برای خواندن ویدیو یا تبدیل این فرمت صوتی، ffmpeg باید روی سرور نصب باشد.") from exc
    try:
        out, _ = await asyncio.wait_for(process.communicate(raw), timeout=35)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise RuntimeError("پردازش رسانه بیش از زمان مجاز طول کشید.") from exc
    if process.returncode or not out:
        raise RuntimeError("استخراج صوت یا تصویر از این فایل ممکن نشد.")
    if len(out) > _max_bytes():
        raise RuntimeError("خروجی پردازش رسانه بیش از حد مجاز است.")
    return out


def _stt_url() -> str:
    url = os.getenv("NOYA_STT_API_URL", "").strip()
    if url:
        return url
    base = os.getenv("NOYA_API_URL", "").strip()
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")] + "/audio/transcriptions"
    return ""


async def transcribe_audio(raw: bytes, *, suffix: str = ".ogg", mime: str = "") -> str:
    """Return text or raise a safe error. Supported speech and audible lyrics depend on provider."""
    if os.getenv("NOYA_STT_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("قابلیت تبدیل صدا به متن غیرفعال است.")
    url = _stt_url()
    if not url:
        raise RuntimeError("آدرس سرویس تبدیل صدا به متن تنظیم نشده است.")
    key = os.getenv("NOYA_STT_API_KEY", "").strip() or os.getenv("NOYA_API_KEY", "").strip()
    model = os.getenv("NOYA_STT_MODEL", "whisper-1").strip() or "whisper-1"
    suffix = suffix if suffix in AUDIO_SUFFIXES else ".ogg"
    # Always send an allowlisted name, never the user-supplied filename.
    content_type = mime if mime.startswith("audio/") else "application/octet-stream"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    timeout = httpx.Timeout(_limit("NOYA_STT_TIMEOUT", 90, 10, 240))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url, headers=headers, data={"model": model, "response_format": "json"},
                files={"file": (f"noya_input{suffix}", raw, content_type)},
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("STT provider rejected media status=%s", exc.response.status_code)
        raise RuntimeError(f"سرویس تبدیل صدا پاسخ ناموفق داد (HTTP {exc.response.status_code}).") from exc
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("STT provider unavailable: %s", type(exc).__name__)
        raise RuntimeError("اتصال به سرویس تبدیل صدا به متن یا پاسخ آن نامعتبر بود.") from exc
    text = data.get("text", "") if isinstance(data, dict) else ""
    if not isinstance(text, str):
        raise RuntimeError("پاسخ سرویس تبدیل صدا معتبر نبود.")
    return text.strip()[:_limit("NOYA_MEDIA_MAX_TEXT_CHARS", 16000, 1000, 32000)]


def _read_document(raw: bytes, suffix: str) -> str:
    cap = _limit("NOYA_MEDIA_MAX_TEXT_CHARS", 16000, 1000, 32000)
    if len(raw) > 8_000_000:
        raise ValueError("برای استخراج متن، فایل سند باید کوچک‌تر از ۸ مگابایت باشد.")
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:12])
    elif suffix == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(raw))
        text = "\n".join(p.text for p in doc.paragraphs if p.text)
    else:
        if b"\x00" in raw[:4096]:
            raise ValueError("فایل متنی معتبر نیست.")
        text = raw.decode("utf-8-sig", errors="replace")
    return text.strip()[:cap]


async def prepare_media_for_noya(bot, message, *, include_reply: bool = True) -> PreparedMedia:
    """Collect at most one audio/video/document plus existing photos (incl. reply)."""
    prepared = PreparedMedia()
    prepared.images = await collect_noya_images(bot, message, include_reply=include_reply)
    prepared.has_media = bool(prepared.images)
    source, found = "message", _attachment(message)
    if found is None and include_reply:
        source = "reply"
        found = _attachment(getattr(message, "reply_to_message", None))
    if found is None:
        return prepared
    kind, obj, suffix, mime = found
    prepared.has_media = True
    if kind == "unsupported":
        prepared.warnings.append("این نوع فایل پشتیبانی نمی‌شود؛ فایل متنی، PDF، DOCX، تصویر، صدا یا ویدیو بفرست.")
        return prepared
    prepared.has_audio = kind in {"audio", "video"}
    try:
        raw = await _download(bot, obj)
        if kind == "text":
            text = await asyncio.to_thread(_read_document, raw, suffix)
            if not text:
                prepared.warnings.append("متنی قابل استخراج از سند پیدا نشد.")
            else:
                prepared.context = f"[ATTACHED_DOCUMENT source={source} UNTRUSTED]\n{text}\n[/ATTACHED_DOCUMENT]"
        else:
            if kind == "video":
                try:
                    frame = await _ffmpeg(raw, frame=True)
                    normalized_mime, normalized = normalize_image_bytes(frame)
                    if len(prepared.images) < MAX_IMAGES:
                        prepared.images.append({"mime": normalized_mime, "data": normalized, "source": "video_frame"})
                except (RuntimeError, ValueError, OSError) as exc:
                    prepared.warnings.append(f"فریم ویدیو قابل خواندن نبود: {exc}")
                raw = await _ffmpeg(raw, frame=False)
                suffix, mime = ".mp3", "audio/mpeg"
            elif suffix not in {".mp3", ".m4a", ".mp4", ".wav", ".flac", ".webm"}:
                raw = await _ffmpeg(raw, frame=False)
                suffix, mime = ".mp3", "audio/mpeg"
            transcript = await transcribe_audio(raw, suffix=suffix, mime=mime)
            if transcript:
                prepared.transcript = transcript
                prepared.context = f"[AUDIO_TRANSCRIPT source={source} UNTRUSTED]\n{transcript}\n[/AUDIO_TRANSCRIPT]"
            else:
                prepared.warnings.append("در صدا گفتار قابل تشخیصی پیدا نشد (موسیقی بی‌کلام یا صدای نامفهوم ممکن است متن نداشته باشد).")
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
        prepared.warnings.append(str(exc))
    except Exception:
        logger.exception("Unexpected media intake error (no attachment content logged)")
        prepared.warnings.append("خواندن این فایل ناموفق بود.")
    return prepared


def is_transcription_request(text: str) -> bool:
    t = (text or "").casefold().strip()
    return any(phrase in t for phrase in (
        "تبدیل به متن", "تبدیلش به متن", "متن ویس", "متن صدا",
        "پیاده سازی", "پیاده‌سازی", "رونوشت", "transcribe", "transcription",
        "صوت به متن", "ویس به متن", "ویدیو به متن", "متن کن", "متنشو بنویس", "ویس رو بنویس",
    ))
