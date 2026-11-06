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
import os
import sys

logger = logging.getLogger(__name__)

MAX_SAMPLE_FRAMES = 3
MAX_ANIMATION_BYTES = 20_000_000
MAX_RENDER_BYTES = 12_000_000
_RENDER_TIMEOUT_SECONDS = 18


class MediaDecodeError(RuntimeError):
    """A safe, stable decoder error code; never includes ffmpeg stderr."""

    def __init__(self, code: str):
        super().__init__("Animated media decoding unavailable")
        self.code = code


async def _communicate(
    argv: list[str], raw: bytes, *, timeout: int, seekable: bool = False,
) -> bytes:
    """Run a bounded subprocess, optionally using a seekable anonymous memfd.

    Telegram MP4/WEBM files may have metadata after their video frames and
    require seeks that 'pipe:0' cannot provide. On Ubuntu a Linux memfd is
    seekable without creating a pathname or persisting customer media on
    disk. Nothing from stderr is logged; it may contain internal file paths.
    """
    fd = None
    try:
        if seekable:
            if sys.platform != "linux" or not hasattr(os, "memfd_create"):
                raise MediaDecodeError("seekable_unavailable")
            fd = os.memfd_create("noya-temporary-video", flags=getattr(os, "MFD_CLOEXEC", 0))
            with os.fdopen(os.dup(fd), "wb") as temporary:
                temporary.write(raw)
            os.lseek(fd, 0, os.SEEK_SET)
            argv = [
                f"/proc/self/fd/{fd}" if item == "pipe:0" else item
                for item in argv
            ]
            process_args = {
                "stdin": asyncio.subprocess.DEVNULL,
                "pass_fds": (fd,),
            }
            if not any("/proc/self/fd/" in item for item in argv):
                raise MediaDecodeError("missing_input")
        else:
            process_args = {"stdin": asyncio.subprocess.PIPE}
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **process_args,
            )
        except FileNotFoundError as exc:
            raise MediaDecodeError("ffmpeg_not_installed") from exc
        try:
            out, _stderr = await asyncio.wait_for(
                process.communicate(None if seekable else raw), timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise MediaDecodeError("timeout") from exc
        if process.returncode != 0:
            raise MediaDecodeError("ffmpeg_exit")
        if not out or len(out) > MAX_RENDER_BYTES:
            raise MediaDecodeError("invalid_render_output")
        return out
    finally:
        if fd is not None:
            os.close(fd)


def _mjpeg_frames(data: bytes) -> list[bytes]:
    """Parse bounded MJPEG image2pipe output; never treat video as a PNG.

    ffmpeg's APNG-on-stdout output can be non-conformant; Pillow then opens
    it as a single default PNG frame. Extract actual JPEG frame boundaries
    instead, checking that every frame is complete and not oversized.
    """
    frames: list[bytes] = []
    cursor = 0
    while cursor < len(data):
        start = data.find(b"\xff\xd8", cursor)
        if start < 0:
            break
        end = data.find(b"\xff\xd9", start + 2)
        if end < 0:
            break
        frame = data[start:end + 2]
        if len(frame) > 3_500_000:
            raise RuntimeError("Oversized sampled video frame")
        frames.append(frame)
        if len(frames) > 18:
            raise RuntimeError("Too many decoded frames")
        cursor = end + 2
    if not frames or cursor != len(data):
        raise RuntimeError("Invalid or incomplete MJPEG frame stream")
    return frames


async def _video_to_frames(raw: bytes) -> list[tuple[str, bytes]]:
    """Decode first six seconds to real JPEGs, selecting first/middle/last.

    Bounded ffmpeg subprocess with no shell, no disk files and at most 18
    frames at 512px. Sampling directly into MJPEG avoids broken APNG pipes.
    """
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-t", "6", "-an",
        "-vf", "fps=3,scale=512:-2:flags=lanczos",
        "-frames:v", "18", "-f", "image2pipe",
        "-vcodec", "mjpeg", "pipe:1",
    ]
    try:
        decoded = _mjpeg_frames(
            await _communicate(command, raw, timeout=_RENDER_TIMEOUT_SECONDS),
        )
        mode = "stream"
    except (RuntimeError, ValueError, OSError) as first:
        reason = str(getattr(first, "code", "") or type(first).__name__)[:48]
        logger.warning(
            "[NOYA-ANIMATION] stage=video_decode result=stream_failed code=%s",
            reason,
        )
        # Timeout or missing decoder cannot be repaired by changing the
        # input transport: don't spend another 18 seconds trying.
        if reason in {"timeout", "ffmpeg_not_installed"}:
            raise
        try:
            decoded = _mjpeg_frames(
                await _communicate(
                    command, raw, timeout=_RENDER_TIMEOUT_SECONDS, seekable=True,
                ),
            )
            mode = "seekable_memfd"
        except (RuntimeError, ValueError, OSError) as second:
            logger.warning(
                "[NOYA-ANIMATION] stage=video_decode result=seekable_failed "
                "code=%s error_type=%s",
                str(getattr(second, "code", "") or type(second).__name__)[:48],
                type(second).__name__,
            )
            raise MediaDecodeError("video_decode_failed") from second
    selected = sorted({0, (len(decoded) - 1) // 2, len(decoded) - 1})
    logger.info(
        "[NOYA-ANIMATION] stage=video_decode result=ok mode=%s "
        "decoded_frames=%d selected_frames=%d",
        mode, len(decoded), len(selected),
    )
    return [("image/jpeg", decoded[index]) for index in selected]


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
        return await _video_to_frames(raw)
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
        len(raw_frame) > 3_500_000 or not raw_frame.startswith(b"\xff\xd8")
        for _mime, raw_frame in frames
    ):
        raise RuntimeError("فریم معتبری از انیمیشن پیدا نشد.")
    return frames
