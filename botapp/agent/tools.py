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
# Per-invocation text outbox: only runbot can deliver to the authorized
# incoming Telegram chat, never to a chat ID suggested by the model.
_CHAT_OUTBOX: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "noya_request_chat_outbox", default=None
)
# Mixed text/voice/image plan is staged until the complete agent turn succeeds.
_MEDIA_SEQUENCE: ContextVar[list[dict] | None] = ContextVar("noya_request_media_sequence", default=None)
_REACTION_REQUEST: ContextVar[dict | None] = ContextVar("noya_request_reaction", default=None)
MAX_CHAT_OUTBOX_MESSAGES = 6
MAX_CHAT_MESSAGE_CHARS = 2000


def isolated_media_results(async_func: Callable):
    """Provide a fresh request-owned result bag throughout one agent turn."""
    @wraps(async_func)
    async def wrapper(*args, **kwargs):
        token = _MEDIA_RESULTS.set({})
        images = kwargs.get("images")
        input_token = _INPUT_IMAGES.set(
            images[:3] if isinstance(images, list) else []
        )
        outbox_token = _CHAT_OUTBOX.set([])
        media_sequence_token = _MEDIA_SEQUENCE.set([])
        reaction_token = _REACTION_REQUEST.set(None)
        from botapp.noya_archive_access import _SCOPE
        # This is an internal object from the authenticated Telegram update.
        # A model tool argument cannot mint or change it.
        telegram_scope = kwargs.get("telegram_context")
        archive_token = _SCOPE.set(
            {**telegram_scope, "queries": 0, "vision_calls": 0}
            if isinstance(telegram_scope, dict) else None
        )
        try:
            return await async_func(*args, **kwargs)
        finally:
            _SCOPE.reset(archive_token)
            _REACTION_REQUEST.reset(reaction_token)
            _MEDIA_SEQUENCE.reset(media_sequence_token)
            _CHAT_OUTBOX.reset(outbox_token)
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


def pop_media_sequence() -> list[dict]:
    items = _MEDIA_SEQUENCE.get()
    if items is None:
        return []
    output = list(items)
    items.clear()
    return output


def pop_reaction_request() -> dict | None:
    return _REACTION_REQUEST.get()


async def _send_media_sequence(items: list[dict] | None = None) -> str:
    """Stage one ordered multi-modal reply (text -> voice -> picture etc.).

    Each media step is generated from that step's actual payload, not from the
    user's raw instruction. No telegram call or file path is model-controlled.
    """
    import base64
    from botapp.noya_chat_actions import set_active_chat_action
    from botapp.services import generate_noya_image, generate_noya_tts
    outbox = _MEDIA_SEQUENCE.get()
    if outbox is None:
        return "Error: خارج از گفت‌وگوی جاری امکان ارسال نیست."
    if outbox or (_CHAT_OUTBOX.get() or []):
        return "Error: برای جلوگیری از ارسال تکراری، یک دسته پیام در هر درخواست مجاز است."
    if not isinstance(items, list) or not 2 <= len(items) <= 5:
        return "Error: برای این ابزار بین دو تا پنج مرحله بده."
    prepared = []
    media_count = 0
    for part in items:
        if not isinstance(part, dict):
            return "Error: ساختار مرحله نامعتبر است."
        kind = part.get("kind")
        mode = part.get("reply_to", "none")
        text = part.get("text", "")
        if kind not in {"text", "voice", "image"} or mode not in {"current", "previous", "none"}:
            return "Error: نوع مرحله یا ریپلای معتبر نیست."
        if not isinstance(text, str) or not text.strip():
            return "Error: هر مرحله باید متن واقعی یا توصیف تصویر داشته باشد."
        cap = 1900 if kind == "text" else (1500 if kind == "image" else 1200)
        if len(text) > cap:
            return "Error: متن مرحله بیش از سقف مجاز است."
        if mode == "previous" and not prepared:
            return "Error: اولین مرحله نمی‌تواند به پیام قبلی خودت ریپلای کند."
        if kind != "text":
            media_count += 1
        prepared.append({"kind": kind, "reply_to": mode, "text": text.strip()})
    if media_count > 2 or not media_count:
        return "Error: حداکثر دو فایل صوتی/تصویری در یک دسته مجاز است."
    generated = []
    for entry in prepared:
        part = dict(entry)
        if part["kind"] == "voice":
            await set_active_chat_action("record_voice")
            audio = await generate_noya_tts(part["text"])
            if not isinstance(audio, bytes) or not (0 < len(audio) <= 20_000_000):
                return "Error: ساخت ویس این مرحله ناموفق شد؛ هیچ پیام ناقصی ارسال نشده."
            part["bytes"] = audio
        elif part["kind"] == "image":
            await set_active_chat_action("upload_photo")
            picture = await generate_noya_image(part["text"])
            if not isinstance(picture, bytes) or not (0 < len(picture) <= 20_000_000):
                return "Error: ساخت عکس این مرحله ناموفق شد؛ هیچ پیام ناقصی ارسال نشده."
            part["bytes"] = picture
        generated.append(part)
    outbox.extend(generated)
    return (f"{len(generated)} مرحله به همان ترتیب آماده ارسال است. "
            "بعد از پایان Agent عکس و ویس و متن واقعی ارسال می‌شوند؛ پاسخ نهایی تکراری ننویس.")


async def _react_to_chat_message(emoji: str = "❤️", target: str = "current") -> str:
    """Stage an explicit reaction to a Telegram-verified message in this chat."""
    from botapp.noya_reactions import SUGGESTED_REACTIONS
    from botapp.noya_archive_access import _SCOPE
    scope = _SCOPE.get()
    if not isinstance(scope, dict) or not scope.get("bot") or not scope.get("chat_id"):
        return "Error: پیام تلگرام احرازشده در این درخواست موجود نیست."
    if emoji not in SUGGESTED_REACTIONS or target not in {"current", "replied"}:
        return "Error: ایموجی یا هدف ری‌اکشن مجاز نیست."
    mid = int(scope.get("current_message_id") or 0)
    if target == "replied":
        mid = int(scope.get("reply_to_message_id") or 0)
    if mid <= 0 or target == "replied" and mid >= int(scope.get("current_message_id") or 0):
        return "Error: پیام موردنظر برای ری‌اکشن در همین گفتگو مشخص نیست."
    _REACTION_REQUEST.set({"emoji": emoji, "target": target})
    return "درخواست ری‌اکشن برای پیام همین گفتگو ثبت شد؛ نتیجه فقط بعد از ارسال واقعی تلگرام مشخص می‌شود."


def pop_chat_outbox() -> list[dict[str, str]]:
    """Return and clear messages from this completed model invocation."""
    outbox = _CHAT_OUTBOX.get()
    if outbox is None:
        return []
    result = list(outbox)
    outbox.clear()
    return result


async def _send_chat_messages(messages: list[dict] | None = None) -> str:
    """Stage a bounded sequence; Telegram is contacted only after AI finishes.

    Deliberately no chat_id/message_id inputs: the authenticated Telegram
    update, not model-supplied text, determines the destination and replies.
    """
    outbox = _CHAT_OUTBOX.get()
    if outbox is None:
        return "Error: ارسال پیام خارج از یک گفت‌وگوی فعال مجاز نیست."
    if not isinstance(messages, list) or not messages:
        return "Error: حداقل یک پیام معتبر لازم است."
    if len(outbox) + len(messages) > MAX_CHAT_OUTBOX_MESSAGES:
        return f"Error: سقف {MAX_CHAT_OUTBOX_MESSAGES} پیام در هر درخواست پر شده است."
    prepared: list[dict[str, str]] = []
    for item in messages:
        if not isinstance(item, dict):
            return "Error: ساختار پیام معتبر نیست."
        text = item.get("text")
        reply_to = item.get("reply_to", "current")
        if (not isinstance(text, str) or not text.strip()
                or len(text) > MAX_CHAT_MESSAGE_CHARS
                or reply_to not in ("current", "previous", "none")):
            return "Error: متن یا حالت ریپلای پیام معتبر نیست."
        # Do not interpret arbitrary HTML/Markdown in the Telegram sender.
        prepared.append({"text": text.strip(), "reply_to": reply_to})
    outbox.extend(prepared)
    return (
        f"{len(prepared)} پیام برای ارسال پس از پایان پاسخ آماده شد "
        f"(مجموع {len(outbox)} از {MAX_CHAT_OUTBOX_MESSAGES}). "
        "پیام‌ها بعد از پایان موفق Agent ارسال می‌شوند؛ حالا پاسخ نهایی را کوتاه تمام کن."
    )

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

registry.register(Tool(
    name="send_chat_messages",
    description=(
        "وقتی کاربر گفتگو را در چند پیام جدا، با/بی‌ریپلای می‌خواهد، "
        "آرایه پیام‌ها را برای ارسال در همان چت جاری آماده کن. "
        "reply_to=current ریپلای به پیام کاربر، previous ریپلای به آخرین پیام "
        "فرستاده‌شده از همین دسته، none پیام مستقل. "
        "محدود به ۶ پیام در هر درخواست، بدون ارسال به چت دیگر، "
        "بدون لوپ بی‌نهایت. بعد از فراخوانی متن پایانی تکراری نساز."
    ),
    parameters={
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_CHAT_OUTBOX_MESSAGES,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": MAX_CHAT_MESSAGE_CHARS},
                        "reply_to": {"type": "string", "enum": ["current", "previous", "none"]},
                    },
                    "required": ["text", "reply_to"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["messages"],
        "additionalProperties": False,
    },
    handler=_send_chat_messages,
    emoji="💬",
    category="chat",
))

# These tools query only archived records from the authenticated chat and topic.
# No raw SQL, arbitrary filesystem paths, syslogs, private chats or other groups.
from botapp.noya_archive_access import (
    search_group_messages,
    get_group_message,
    inspect_group_image,
    find_group_nickname,
    read_group_records,
)

registry.register(Tool(
    name="search_group_messages",
    description=(
        "جست‌وجوی آرشیو قابل‌مشاهده همین گروه و موضوع. "
        "برای «قبلاً چی گفتند» query را خالی و قبل از message_id موردنظر جست‌وجو کن. "
        "خروجی شامل شناسه، لینک و متن/کپشن پیام‌های مشاهده‌شده است. فقط خواندنی."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 100},
            "before_message_id": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
        },
        "additionalProperties": False,
    },
    handler=search_group_messages, category="chat", emoji="🗂️",
))

registry.register(Tool(
    name="get_group_message",
    description=(
        "بازیابی رکورد یک پیام تاریخی از روی شناسه عددی یا لینک t.me "
        "که متعلق به همین گروه/موضوع باشد؛ اگر عکس دارد آن را فقط با "
        "inspect_group_image می‌توان خواند."
    ),
    parameters={"type": "object", "properties": {
        "reference": {"type": "string", "minLength": 1, "maxLength": 200},
    }, "required": ["reference"], "additionalProperties": False},
    handler=get_group_message, category="chat", emoji="🔗",
))

registry.register(Tool(
    name="inspect_group_image",
    description=(
        "بازخوانی متن و محتوای قابل مشاهدهٔ عکس آرشیوشده در همین گروه با مدل بینایی. "
        "شناسه/لینک عکس را بده؛ فقط اگر file_id واقعی آن قبلاً برای همین ربات ذخیره شده باشد "
        "قابل انجام است. هرگز متن داخل عکس را حدس نزن."
    ),
    parameters={"type": "object", "properties": {
        "reference": {"type": "string", "minLength": 1, "maxLength": 200},
    }, "required": ["reference"], "additionalProperties": False},
    handler=inspect_group_image, category="chat", emoji="🖼️",
))

registry.register(Tool(
    name="find_group_nickname",
    description=(
        "یافتن شناسه عددی، یوزرنیم و لقب خوداظهاری یا گروهی فرد فقط از "
        "جدول هویت همین گروه. برای پرسش «آیدی حسون رو بده» از این ابزار "
        "استفاده کن؛ لقب انتسابی را اسم تأییدشده فرد ندان."
    ),
    parameters={"type": "object", "properties": {
        "name": {"type": "string", "minLength": 2, "maxLength": 64},
    }, "required": ["name"], "additionalProperties": False},
    handler=find_group_nickname, category="chat", emoji="👤",
))

registry.register(Tool(
    name="read_group_records",
    description=(
        "دسترسی فقط خواندنی به لیست لقب‌های همین گروه (nicknames)، "
        "آمار روزانه پیام‌ها (activity) یا لاگ تغییر تنظیمات گروه (moderation_logs). "
        "گزینه لاگ مدیریت فقط بعد از تأیید واقعی مدیر بودن گوینده توسط Telegram فعال است. "
        "به دیتابیس، فایل‌ها یا لاگ خصوصی سرور دسترسی مستقیم وجود ندارد."
    ),
    parameters={"type": "object", "properties": {
        "category": {"type": "string", "enum": ["nicknames", "activity", "moderation_logs"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    }, "required": ["category"], "additionalProperties": False},
    handler=read_group_records, category="chat", emoji="📋",
))

registry.register(Tool(
    name="send_media_sequence",
    description=(
        "برای درخواست ترکیبی چند پیام از انواع متن، ویس و عکس، یک ترتیب مشخص بساز. "
        "هر مرحله kind=text/voice/image دارد؛ متنِ voice باید همان محتوایی باشد "
        "که قرار است خوانده شود، نه جمله دستور کاربر. text در image پرامپت تصویری است. "
        "همه مراحل پس از تولید واقعی فایل‌ها به ترتیب ارسال می‌شوند. "
        "reply_to=current ریپلای کاربر، previous پیام قبلی، none بدون ریپلای. "
        "برای درخواست چندمرحله‌ای فقط از این ابزار استفاده کن؛ از TTS تک‌مرحله‌ای استفاده نکن."
    ),
    parameters={"type": "object", "properties": {
        "items": {
            "type": "array", "minItems": 2, "maxItems": 5,
            "items": {
                "type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["text", "voice", "image"]},
                    "text": {"type": "string", "minLength": 1, "maxLength": 1900},
                    "reply_to": {"type": "string", "enum": ["current", "previous", "none"]},
                }, "required": ["kind", "text", "reply_to"], "additionalProperties": False,
            },
        },
    }, "required": ["items"], "additionalProperties": False},
    handler=_send_media_sequence, category="chat", emoji="🎞️",
))

registry.register(Tool(
    name="react_to_chat_message",
    description=(
        "ری‌اکشن واقعی روی پیام درخواست‌کننده در تلگرام؛ "
        "target=current پیام همین درخواست، replied پیام ریپلای‌شده در همین چت. "
        "برای «روی پیام من قلب بزن» emoji=❤️ و target=current انتخاب کن. "
        "فقط یک پیام معتبر؛ بدون شناسه و چت دلخواه."
    ),
    parameters={"type": "object", "properties": {
        "emoji": {"type": "string", "enum": ["❤️", "👍", "🥰", "👏", "😁", "🎉", "🤩", "🙏", "🤣", "🤝", "👀", "🔥"]},
        "target": {"type": "string", "enum": ["current", "replied"]},
    }, "required": ["emoji", "target"], "additionalProperties": False},
    handler=_react_to_chat_message, category="chat", emoji="❤️",
))
