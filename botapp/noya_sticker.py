"""Image-to-static-Telegram-sticker conversion (request-local, no persisted media)."""
from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError


def image_to_webp_sticker(raw: bytes) -> bytes:
    """Make a Telegram-compatible static WebP sticker.

    At least one dimension is exactly 512px; preserve aspect ratio and alpha.
    Reject oversized image data and decompression bombs.
    """
    if not raw or len(raw) > 20_000_000:
        raise ValueError("حجم تصویر برای استیکر بیش از حد مجاز است.")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > 25_000_000:
                raise ValueError("ابعاد تصویر برای استیکر بیش از حد بزرگ است.")
            source.load()
            frame = ImageOps.exif_transpose(source)
            rgba = frame.convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("تصویر برای تبدیل به استیکر قابل خواندن نیست.") from exc
    rgba.thumbnail((512, 512), Image.Resampling.LANCZOS)
    if rgba.width == 0 or rgba.height == 0:
        raise ValueError("ابعاد تصویر نامعتبر است.")
    # Telegram requires one dimension to be 512. Preserve transparent padding.
    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    canvas.alpha_composite(rgba, ((512 - rgba.width) // 2, (512 - rgba.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, format="WEBP", quality=88, method=4)
    result = buf.getvalue()
    if len(result) > 512 * 1024:
        buf = io.BytesIO()
        canvas.save(buf, format="WEBP", quality=70, method=4)
        result = buf.getvalue()
    if len(result) > 512 * 1024:
        raise ValueError("حجم خروجی استیکر از سقف تلگرام بیشتر شد.")
    return result
