"""Noya agent tools — Hermes-style registered tools."""
from __future__ import annotations
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from .registry import Tool, registry

TEHRAN_TZ = ZoneInfo("Asia/Tehran")
IRST = timezone(timedelta(hours=3, minutes=30))

# Keep binary results inside the *current agent invocation*, never in a
# process-global dict with a shared key (cross-user result leak).
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable

_MEDIA_RESULTS: ContextVar[dict[str, Any] | None] = ContextVar(
    "noya_request_media_results", default=None
)
# Read-only, per-turn media from the message the user actually supplied.
# Never accept file paths, Telegram IDs or external URLs from the LLM.
_INPUT_IMAGES: ContextVar[list[dict] | None] = ContextVar(
    "noya_request_input_images", default=None
)


def isolated_media_results(async_func: Callable):
    """Provide a fresh request-owned result bag throughout one agent turn."""
    @wraps(async_func)
    async def wrapper(*args, **kwargs):
        token = _MEDIA_RESULTS.set({})
        images = kwargs.get("images")
        input_token = _INPUT_IMAGES.set(
            images[:3] if isinstance(images, list) else []
        )
        try:
            return await async_func(*args, **kwargs)
        finally:
            _INPUT_IMAGES.reset(input_token)
            _MEDIA_RESULTS.reset(token)
    return wrapper


def _store_media_result(kind: str, payload: Any) -> None:
    results = _MEDIA_RESULTS.get()
    if results is not None:
        results[kind] = payload


def pop_media_result(kind: str):
    results = _MEDIA_RESULTS.get()
    return results.pop(kind, None) if results is not None else None

# ── URL helpers ──
_URL_RE = re.compile(r'https?://[^\s<>"\']+')
HTML_SCRIPTS = re.compile(r'<script[^>]*>.*?</script>', re.S | re.I)
HTML_STYLES = re.compile(r'<style[^>]*>.*?</style>', re.S | re.I)
HTML_TAGS = re.compile(r'<[^>]+>')
HTML_COMMENTS = re.compile(r'<!--.*?-->', re.S)
MULTI_NEWLINES = re.compile(r'\n\s*\n')


def extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text)


def _strip_html(html: str) -> str:
    text = HTML_SCRIPTS.sub('', html)
    text = HTML_STYLES.sub('', text)
    text = HTML_COMMENTS.sub('', text)
    text = HTML_TAGS.sub(' ', text)
    text = MULTI_NEWLINES.sub('\n', text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return '\n'.join(lines)[:8000]


# ── Tool handlers ──

async def _web_search(query: str = "") -> str:
    """Search the live web and render results with their SOURCE dates.

    Every line states the source publication date, or ``date=unknown`` when the
    source published none. A stale result set (explicitly asked for the latest,
    but nothing is recent) is flagged with a ``recency_warning`` line rather
    than silently presented as current. No results means an honest failure
    string — never invented content.
    """
    from urllib.parse import urlparse

    from botapp.web import default_web_service
    from botapp.web.dates import format_source_date
    from botapp.web.policy import freshness_intent
    from botapp.web.ranking import recency_summary

    clean_q = (query or "").strip()
    if not clean_q:
        return "لطفاً عبارت جستجو را مشخص کنید."

    freshness = freshness_intent(clean_q)
    results = await default_web_service.search(clean_q, limit=5, freshness=freshness)
    if not results:
        return "نتیجه‌ای در اینترنت یافت نشد."

    lines: list[str] = []
    for r in results[:5]:
        title = " ".join((r.title or "").split()) or "(بدون عنوان)"
        try:
            host = urlparse(r.url or "").netloc.lower()
        except Exception:
            host = ""
        if host.startswith("www."):
            host = host[4:]
        published = format_source_date(getattr(r, "published_at", "")) or "unknown"
        lines.append(f"• {title} | date={published} | source={host or r.source or 'unknown'}")
        snippet = " ".join((r.snippet or "").split())
        if snippet:
            lines.append(f"  {snippet[:240]}")
        if r.url:
            lines.append(f"  {r.url}")

    summary = recency_summary(results, freshness)
    if freshness in ("latest", "recent") and summary.get("stale") and summary.get("oldest_days") is not None:
        lines.append(f"recency_warning={summary['oldest_days']}d")
    return "\n".join(lines)


async def _fetch_url(url: str = "", query: str = "") -> str:
    from botapp.web import default_web_service
    from botapp.web.dates import format_source_date
    from botapp.web.evidence import EvidenceBuilder

    target = (url or "").strip()
    if not target:
        return "لطفاً آدرس لینک را وارد کنید."
    try:
        doc = await default_web_service.fetch_page(target)
        # "تاریخ انتشار" here is the PAGE's own publication date. It is
        # explicitly not the date of any event mentioned on the page, and it is
        # "نامشخص" when the page declares nothing.
        published = format_source_date(getattr(doc, "published_at", "")) or "نامشخص"
        header = (
            f"عنوان صفحه: {doc.title}\n"
            f"آدرس: {doc.final_url}\n"
            f"تاریخ انتشار: {published}"
            f" (تاریخ انتشار خودِ صفحه است، نه تاریخ وقوع رویداد)\n"
        )
        if len(doc.text) <= 8000:
            body = doc.text
        else:
            best_chunks = EvidenceBuilder.select_best_chunks(doc, query=query, max_chunks=5)
            if best_chunks:
                body = "\n\n---\n\n".join(f"[بخش {c.source_id}]:\n{c.text}" for c in best_chunks)
            else:
                body = doc.text[:8000]
        return f"{header}\n{body}" if body else "صفحه خالی بود."
    except Exception as exc:
        return f"خطا در باز کردن لینک: {exc}"


def _get_time(**_extra) -> str:
    now = datetime.now(TEHRAN_TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S (%A)")


async def _generate_image(prompt: str = "") -> str:
    from botapp.services import generate_noya_image
    from botapp.noya_chat_actions import set_active_chat_action
    await set_active_chat_action("upload_photo")
    img_bytes = await generate_noya_image(prompt)
    if not img_bytes:
        return "خطا در تولید تصویر."
    # Store raw bytes for runbot to pick up, return text for LLM
    import base64
    _b64 = base64.b64encode(img_bytes).decode()
    _store_media_result("image", _b64)
    return f"تصویر با موفقیت تولید شد ({len(img_bytes)} bytes). در حال ارسال..."


async def _text_to_speech(text: str = "") -> str:
    from botapp.services import generate_noya_tts
    from botapp.noya_chat_actions import set_active_chat_action
    await set_active_chat_action("record_voice")
    audio = await generate_noya_tts(text)
    if not audio:
        return "خطا در تولید صدا."
    _store_media_result("tts", audio)
    return f"صدا با موفقیت تولید شد ({len(audio)} bytes). در حال ارسال..."


async def _edit_image(instruction: str = "") -> str:
    """Edit only a photo explicitly available in this user's current turn."""
    from botapp.services import edit_noya_image
    from botapp.noya_chat_actions import set_active_chat_action
    images = _INPUT_IMAGES.get() or []
    target = next(
        (img for img in images
         if isinstance(img, dict) and img.get("source") in ("reply", "reply_parent")
         and isinstance(img.get("data"), bytes)),
        None,
    )
    if target is None:
        target = next(
            (img for img in images
             if isinstance(img, dict) and isinstance(img.get("data"), bytes)),
            None,
        )
    if target is None:
        return "برای ویرایش، کاربر باید عکس را ارسال کند یا روی عکس ریپلای بزند."
    source = target["data"]
    mime = target.get("mime", "image/jpeg")
    if (not source or len(source) > 3_500_000
            or mime not in ("image/jpeg", "image/png", "image/webp")):
        return "تصویر برای ویرایش پشتیبانی نمی‌شود."
    if not instruction.strip():
        return "لطفاً تغییر موردنظر برای عکس را مشخص کن."
    await set_active_chat_action("upload_photo")
    edited = await edit_noya_image(instruction.strip(), source, mime)
    if not edited:
        return "ویرایش تصویر موفق نبود؛ به کاربر بگو دوباره تلاش کند."
    import base64
    _store_media_result("image", base64.b64encode(edited).decode("ascii"))
    return "تصویر ویرایش شد و برای ارسال به کاربر آماده است."



# ── Registration ──

registry.register(Tool(
    name="web_search",
    description="جستجوی اطلاعات در اینترنت. از DuckDuckGo استفاده می‌کند.",
    parameters={"type": "object", "properties": {"query": {"type": "string", "description": "متن جستجو"}}, "required": ["query"]},
    handler=_web_search, emoji="🔍",
    category="chat",
))

registry.register(Tool(
    name="fetch_url",
    description="خواندن و تحلیل محتوای یک صفحه وب. لینک را بده تا محتوا را بخوانم.",
    parameters={"type": "object", "properties": {"url": {"type": "string", "description": "آدرس URL"}}, "required": ["url"]},
    handler=_fetch_url, emoji="🌐",
    category="chat",
))

registry.register(Tool(
    name="get_time",
    description="اعلام ساعت و تاریخ فعلی به وقت تهران.",
    parameters={"type": "object", "properties": {}},
    handler=_get_time, emoji="⏰",
    category="chat",
))

registry.register(Tool(
    name="generate_image",
    description="تولید تصویر با هوش مصنوعی. توصیف را به انگلیسی بنویس.",
    parameters={"type": "object", "properties": {"prompt": {"type": "string", "description": "توصیف تصویر به انگلیسی"}}, "required": ["prompt"]},
    handler=_generate_image, emoji="🎨",
    category="chat",
))

registry.register(Tool(
    name="text_to_speech",
    description="تبدیل متن به صدا. متن فارسی یا انگلیسی بده.",
    parameters={"type": "object", "properties": {"text": {"type": "string", "description": "متن برای تبدیل به صدا"}}, "required": ["text"]},
    handler=_text_to_speech, emoji="🔊",
    category="chat",
))

registry.register(Tool(
    name="edit_image",
    description=(
        "ویرایش عکس همین پیام یا عکس ریپلای‌شده. "
        "تغییر دلخواه را دقیق توضیح بده؛ تصویر اصلی خودکار و ایمن از پیام جاری گرفته می‌شود. "
        "اگر عکس نداریم، این ابزار نمی‌تواند تصویر بسازد؛ برای تصویر تازه از generate_image استفاده کن."
    ),
    parameters={
        "type": "object",
        "properties": {"instruction": {"type": "string", "description": "دستور تغییر تصویر", "minLength": 1, "maxLength": 2000}},
        "required": ["instruction"],
        "additionalProperties": False,
    },
    handler=_edit_image, emoji="✏️",
    category="chat",
))
