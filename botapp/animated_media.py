"""Safely produce a small chronological set of still frames for model vision.

GIF/APNG/WebP are sampled in a short-lived Python worker. Telegram video
stickers/GIF-as-MP4 are decoded by ffmpeg first. TGS is rendered via rlottie in
the same isolated worker. Neither raw videos nor frames are persisted.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys

logger = logging.getLogger(__name__)

MAX_SAMPLE_FRAMES = 3
MAX_ANIMATION_BYTES = 20_000_000
MAX_RENDER_BYTES = 12_000_000
_RENDER_TIMEOUT_SECONDS = 18


async def _communicate(argv: list[str], raw: bytes, *, timeout: int) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ابزار پردازش انیمیشن روی سرور نصب نیست.") from exc
    try:
        out, _stderr = await asyncio.wait_for(process.communicate(raw), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise RuntimeError("پردازش استیکر یا GIF بیش از حد طول کشید.") from exc
    if process.returncode != 0:
        raise RuntimeError("استخراج فریم‌های انیمیشن ناموفق بود.")
    if not out or len(out) > MAX_RENDER_BYTES:
        raise RuntimeError("خروجی انیمیشن نامعتبر یا بیش از حد بزرگ است.")
    return out


async def _video_to_apng(raw: bytes) -> bytes:
    """Decode the first six seconds, up to 18 frames, without invoking a shell."""
    return await _communicate(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-t", "6", "-an",
            "-vf", "fps=3,scale=512:-2:flags=lanczos",
            "-f", "apng", "pipe:1",
        ],
        raw, timeout=_RENDER_TIMEOUT_SECONDS,
    )


async def sample_animation_frames(raw: bytes, *, kind: str) -> list[tuple[str, bytes]]:
    """Return at most 3 JPEG frames: start, middle and end, in that order.

    kind: 'image' for GIF/animated WebP/APNG, 'video' for WEBM/MP4, 'tgs'
    for Telegram Lottie stickers. Raises on failure so caller can use a
    Telegram thumbnail as a transparent fallback.
    """
    if kind not in {"image", "video", "tgs"}:
        raise ValueError("Unsupported animation kind")
    if not raw or len(raw) > MAX_ANIMATION_BYTES:
        raise ValueError("Animation exceeds the maximum input size")
    if kind == "video":
        raw = await _video_to_apng(raw)
        kind = "image"
    out = await _communicate(
        [sys.executable, "-m", "botapp.tgs_renderer", kind],
        raw, timeout=_RENDER_TIMEOUT_SECONDS,
    )
    try:
        data = json.loads(out)
        frames = [
            ("image/jpeg", base64.b64decode(item, validate=True))
            for item in data["frames"]
        ]
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("پاسخ پردازش انیمیشن نامعتبر بود.") from exc
    if not frames or len(frames) > MAX_SAMPLE_FRAMES or any(
        len(raw_frame) > 3_500_000 or not raw_frame.startswith(b"\\xff\\xd8")
        for _mime, raw_frame in frames
    ):
        raise RuntimeError("فریم معتبری از انیمیشن پیدا نشد.")
    return frames
