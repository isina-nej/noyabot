"""Subprocess-only Telegram animation renderer.

Reads bytes from stdin and prints JSON base64 of up to 3 normalized JPEGs.
No shell or network use. Native rlottie is kept out of the long-lived bot.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import zlib


def _frame_numbers(total: int) -> list[int]:
    if total < 1:
        return []
    return sorted({0, (total - 1) // 2, total - 1})


def _limit_worker_resources() -> None:
    if sys.platform != "linux":
        return
    import resource
    # Malformed animations should never exhaust the bot host. System memory is
    # 3.8 GiB on the current deployment, so each worker is limited to 512 MiB.
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))


def _bounded_tgs_json(raw: bytes) -> str:
    if len(raw) > 256_000:
        raise ValueError("TGS file too large")
    decompress = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = decompress.decompress(raw, 1_000_001)
    if len(data) > 1_000_000 or not decompress.eof or decompress.unused_data:
        raise ValueError("Invalid or oversized TGS data")
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("Invalid animation structure")
    width = int(parsed.get("w", 0))
    height = int(parsed.get("h", 0))
    start = int(parsed.get("ip", 0))
    stop = int(parsed.get("op", 0))
    if not 1 <= width <= 512 or not 1 <= height <= 512 or not 1 <= stop - start <= 240:
        raise ValueError("Unsupported TGS dimensions or frame count")
    return data.decode("utf-8")


def _encode_frame(img) -> str:
    from botapp.telegram_media import normalize_image_bytes

    frame = io.BytesIO()
    img.save(frame, format="PNG")
    _mime, jpeg = normalize_image_bytes(frame.getvalue())
    return base64.b64encode(jpeg).decode("ascii")


def render(raw: bytes, kind: str) -> list[str]:
    if kind == "tgs":
        from rlottie_python import LottieAnimation

        data = _bounded_tgs_json(raw)
        with LottieAnimation.from_data(data=data) as animation:
            total = animation.lottie_animation_get_totalframe()
            if total <= 0 or total > 240:
                raise ValueError("Unsupported TGS frame count")
            return [
                _encode_frame(animation.render_pillow_frame(frame_num=n))
                for n in _frame_numbers(total)
            ]
    if kind == "image":
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as animation:
            total = int(getattr(animation, "n_frames", 1))
            if total < 1 or total > 600:
                raise ValueError("Unsupported image frame count")
            if animation.width > 2048 or animation.height > 2048:
                raise ValueError("Oversized animated image dimensions")
            result = []
            for n in _frame_numbers(total):
                animation.seek(n)
                result.append(_encode_frame(animation.convert("RGBA")))
            return result
    raise ValueError("Unknown format")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"image", "tgs"}:
        return 2
    try:
        _limit_worker_resources()
        raw = sys.stdin.buffer.read(20_000_001)
        if not raw or len(raw) > 20_000_000:
            return 2
        frames = render(raw, sys.argv[1])
        sys.stdout.write(json.dumps({"frames": frames}, separators=(",", ":")))
        return 0 if frames else 3
    except Exception:
        return 3


if __name__ == "__main__":
    sys.exit(main())
