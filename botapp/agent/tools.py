"""Noya agent tools — Hermes-style registered tools."""
from __future__ import annotations
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from .registry import Tool, registry

TEHRAN_TZ = ZoneInfo("Asia/Tehran")
IRST = timezone(timedelta(hours=3, minutes=30))

# Shared cache for tool results that need to cross back to runbot.py
_IMAGE_RESULT_CACHE: dict[str, str] = {}
_TTS_RESULT_CACHE: dict[str, bytes] = {}

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
    from botapp.noya_search import maybe_web_search
    result_block = await maybe_web_search(query)
    return result_block if result_block else "نتیجه‌ای یافت نشد."


async def _fetch_url(url: str = "") -> str:
    import httpx
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return _strip_html(resp.text)[:4000]


def _get_time() -> str:
    now = datetime.now(TEHRAN_TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S (%A)")


async def _generate_image(prompt: str = "") -> str:
    from botapp.services import generate_noya_image
    img_bytes = await generate_noya_image(prompt)
    if not img_bytes:
        return "خطا در تولید تصویر."
    # Store raw bytes for runbot to pick up, return text for LLM
    import base64
    _b64 = base64.b64encode(img_bytes).decode()
    _IMAGE_RESULT_CACHE["last"] = _b64
    return f"تصویر با موفقیت تولید شد ({len(img_bytes)} bytes). در حال ارسال..."


async def _text_to_speech(text: str = "") -> str:
    from botapp.services import generate_noya_tts
    audio = await generate_noya_tts(text)
    if not audio:
        return "خطا در تولید صدا."
    _TTS_RESULT_CACHE["last"] = audio
    return f"صدا با موفقیت تولید شد ({len(audio)} bytes). در حال ارسال..."


# ── Registration ──

registry.register(Tool(
    name="web_search",
    description="جستجوی اطلاعات در اینترنت. از DuckDuckGo استفاده می‌کند.",
    parameters={"type": "object", "properties": {"query": {"type": "string", "description": "متن جستجو"}}, "required": ["query"]},
    handler=_web_search, emoji="🔍",
))

registry.register(Tool(
    name="fetch_url",
    description="خواندن و تحلیل محتوای یک صفحه وب. لینک را بده تا محتوا را بخوانم.",
    parameters={"type": "object", "properties": {"url": {"type": "string", "description": "آدرس URL"}}, "required": ["url"]},
    handler=_fetch_url, emoji="🌐",
))

registry.register(Tool(
    name="get_time",
    description="اعلام ساعت و تاریخ فعلی به وقت تهران.",
    parameters={"type": "object", "properties": {}},
    handler=_get_time, emoji="⏰",
))

registry.register(Tool(
    name="generate_image",
    description="تولید تصویر با هوش مصنوعی. توصیف را به انگلیسی بنویس.",
    parameters={"type": "object", "properties": {"prompt": {"type": "string", "description": "توصیف تصویر به انگلیسی"}}, "required": ["prompt"]},
    handler=_generate_image, emoji="🎨",
))

registry.register(Tool(
    name="text_to_speech",
    description="تبدیل متن به صدا. متن فارسی یا انگلیسی بده.",
    parameters={"type": "object", "properties": {"text": {"type": "string", "description": "متن برای تبدیل به صدا"}}, "required": ["text"]},
    handler=_text_to_speech, emoji="🔊",
))
