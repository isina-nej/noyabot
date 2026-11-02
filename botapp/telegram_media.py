"""Bounded Telegram vision intake for photos, animated GIFs and stickers.

Animated formats are sampled at start/middle/end and passed to vision as
chronological JPEG frames; thumbnails remain the last-resort fallback.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_IMAGE_EDGE = 1600
MAX_IMAGE_BYTES = 3_500_000
MAX_IMAGES = 3


def _pil():
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - exercised in deploy
        raise RuntimeError("Pillow is required for Noya vision (pip install Pillow)") from exc
    return Image, ImageOps


def normalize_image_bytes(raw: bytes, *, prefer_mime: str = "image/png") -> tuple[str, bytes]:
    """Return (mime, bytes) as a reasonably sized PNG/JPEG."""
    Image, ImageOps = _pil()
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        # Flatten transparency onto white for JPEG-friendly pipelines.
        if img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, MAX_IMAGE_EDGE / float(max(w, h, 1)))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        # Prefer JPEG for photos (smaller); PNG only if caller insists and small.
        img.save(out, format="JPEG", quality=85, optimize=True)
        data = out.getvalue()
        if len(data) > MAX_IMAGE_BYTES:
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=70, optimize=True)
            data = out.getvalue()
        return "image/jpeg", data


def to_data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


async def _download_file_bytes(bot, file_id: str) -> bytes | None:
    try:
        file = await bot.get_file(file_id)
        if int(getattr(file, "file_size", 0) or 0) > 20_000_000:
            return None
        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf)
        result = buf.getvalue()
        return result if len(result) <= 20_000_000 else None
    except Exception:
        logger.warning("Telegram media download failed", exc_info=True)
        return None


def _largest_photo(message) -> Any | None:
    photos = getattr(message, "photo", None) or []
    if not photos:
        return None
    return max(photos, key=lambda p: int(getattr(p, "file_size", 0) or 0) or (
        int(getattr(p, "width", 0) or 0) * int(getattr(p, "height", 0) or 0)
    ))


async def image_bytes_from_photo_size(bot, photo_size) -> tuple[str, bytes] | None:
    raw = await _download_file_bytes(bot, photo_size.file_id)
    if not raw:
        return None
    try:
        return normalize_image_bytes(raw)
    except Exception:
        logger.exception("Failed to normalize photo")
        return None


async def _animated_frames(raw: bytes, *, kind: str) -> list[tuple[str, bytes]]:
    from botapp.animated_media import sample_animation_frames

    try:
        return await sample_animation_frames(raw, kind=kind)
    except (RuntimeError, ValueError, OSError):
        logger.warning("Failed to render Telegram animation kind=%s; using fallback", kind)
        return []


async def image_frames_from_sticker(bot, sticker) -> list[tuple[str, bytes]]:
    """Render all sampled frames of a Telegram sticker with thumbnail fallback."""
    animated = bool(getattr(sticker, "is_animated", False))
    video = bool(getattr(sticker, "is_video", False))
    full = await _download_file_bytes(bot, sticker.file_id)
    if full:
        if animated or video:
            rendered = await _animated_frames(full, kind="tgs" if animated else "video")
            if rendered:
                return rendered
        else:
            try:
                return [normalize_image_bytes(full)]
            except Exception:
                logger.debug("Static sticker image could not be decoded")
    thumb = getattr(sticker, "thumbnail", None) or getattr(sticker, "thumb", None)
    if thumb is not None and getattr(thumb, "file_id", None):
        raw_thumb = await _download_file_bytes(bot, thumb.file_id)
        if raw_thumb:
            try:
                return [normalize_image_bytes(raw_thumb)]
            except Exception:
                logger.debug("Sticker thumbnail could not be decoded")
    return []


async def image_bytes_from_sticker(bot, sticker) -> tuple[str, bytes] | None:
    """Backward-compatible single-frame interface for callers needing a still."""
    frames = await image_frames_from_sticker(bot, sticker)
    return frames[0] if frames else None


async def image_frames_from_message(bot, message) -> list[tuple[str, bytes]]:
    """Read a photo, sticker, Telegram animation or image document."""
    if message is None:
        return []
    photo = _largest_photo(message)
    if photo is not None:
        got = await image_bytes_from_photo_size(bot, photo)
        return [got] if got else []
    sticker = getattr(message, "sticker", None)
    if sticker is not None:
        return await image_frames_from_sticker(bot, sticker)
    animation = getattr(message, "animation", None)
    if animation is not None:
        raw = await _download_file_bytes(bot, animation.file_id)
        if raw:
            mime = (getattr(animation, "mime_type", None) or "").lower()
            filename = (getattr(animation, "file_name", None) or "").lower()
            kind = "image" if mime == "image/gif" or filename.endswith(".gif") else "video"
            frames = await _animated_frames(raw, kind=kind)
            if frames:
                return frames
            if kind == "image":
                try:
                    return [normalize_image_bytes(raw)]
                except Exception:
                    return []
        return []
    document = getattr(message, "document", None)
    if document is not None:
        mime = (getattr(document, "mime_type", None) or "").lower()
        filename = (getattr(document, "file_name", None) or "").lower()
        if mime.startswith("image/") or filename.endswith((".gif", ".webp", ".png", ".jpg", ".jpeg")):
            raw = await _download_file_bytes(bot, document.file_id)
            if raw:
                if mime == "image/gif" or filename.endswith(".gif"):
                    frames = await _animated_frames(raw, kind="image")
                    if frames:
                        return frames
                try:
                    return [normalize_image_bytes(raw)]
                except Exception:
                    logger.warning("Image document could not be decoded")
    return []


async def image_bytes_from_message(bot, message) -> tuple[str, bytes] | None:
    """Backward-compatible first frame for still-image call sites."""
    frames = await image_frames_from_message(bot, message)
    return frames[0] if frames else None


async def collect_noya_images(bot, message, *, include_reply: bool = True) -> list[dict]:
    """Collect up to three request-local images with temporal frame metadata.

    Animated Telegram media takes priority over images in the reply chain.
    Each frame is a JPEG with an optional frame_index and frame_count.
    """
    out: list[dict] = []
    seen: set[str] = set()

    async def _add(source: str, msg) -> None:
        if len(out) >= MAX_IMAGES or msg is None:
            return
        frames = await image_frames_from_message(bot, msg)
        for index, (mime, data) in enumerate(frames):
            if len(out) >= MAX_IMAGES:
                break
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            item = {"mime": mime, "data": data, "source": source}
            if len(frames) > 1:
                item.update({"frame_index": index + 1, "frame_count": len(frames)})
            out.append(item)

    await _add("message", message)
    if include_reply:
        replied = getattr(message, "reply_to_message", None)
        await _add("reply", replied)
        if replied is not None:
            await _add("reply_parent", getattr(replied, "reply_to_message", None))
    return out


def images_to_data_urls(images: list[dict]) -> list[str]:
    return [to_data_url(img["mime"], img["data"]) for img in images if img.get("data")]
