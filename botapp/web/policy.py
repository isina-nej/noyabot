"""Search decision policy, intent detection, and query normalization."""
from __future__ import annotations

import re
import unicodedata

from .models import SearchDecision

# ── Persian / Arabic normalization ──

_PERSIAN_ARABIC_MAP = {
    "ي": "ی",
    "ى": "ی",
    "ك": "ک",
    "ة": "ه",
    "ۀ": "ه",
    "ؤ": "و",
    "إ": "ا",
    "أ": "ا",
    "ء": "",
}

_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_persian_text(text: str) -> str:
    """Normalize Persian and Arabic variations, digits, and control characters."""
    if not text:
        return ""
    # Normalize unicode composition
    t = unicodedata.normalize("NFC", text)
    # Convert digits
    t = t.translate(_DIGIT_MAP)
    # Replace Arabic characters with Persian equivalents
    for ar, fa in _PERSIAN_ARABIC_MAP.items():
        t = t.replace(ar, fa)
    # Standardize ZWNJ and spaces
    t = re.sub(r"[\u200b\u200e\u200f]", "", t)
    t = re.sub(r"\u200c+", "\u200c", t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return t.strip()


# ── Match patterns ──

EXPLICIT_SEARCH_TRIGGERS = (
    "سرچ کن",
    "سرچش کن",
    "سرچ بزن",
    "سرچش بزن",
    "یه سرچ بزن",
    "جستجو کن",
    "جستجو بزن",
    "جست‌وجو کن",
    "جست‌وجو بزن",
    "تو اینترنت بگرد",
    "تو اینترنت نگاه کن",
    "تو اینترنت ببین",
    "بگرد ببین",
    "پیدا کن",
    "از سایت پیدا کن",
    "بررسی کن",
    "آخرین اطلاعات را پیدا کن",
    "گوگل کن",
    "تو گوگل",
    "search",
    "google",
)

TIME_SENSITIVE_TRIGGERS = (
    "امروز",
    "الان",
    "لحظه ای",
    "لحظه‌ای",
    "جدیدترین",
    "آخرین نسخه",
    "نسخه جدید",
    "آخرین خبر",
    "اخبار",
    "قیمت",
    "نرخ",
    "دلار",
    "طلا",
    "سکه",
    "ارز",
    "بیت کوین",
    "بیت‌کوین",
    "crypto",
    "bitcoin",
    "وضعیت سرویس",
    "قطع شده",
    "خرابه",
    "هواشناسی",
    "آب و هوا",
    "نتیجه بازی",
    "جدول لیگ",
    "برنده انتخابات",
    "release جدید",
    "api جدید",
    "event جاری",
    "تاریخ انتشار",
    "تاریخ عرضه",
    "تاریخ آپدیت",
    "تاریخ بروزرسانی",
    "release date",
    "release notes",
)

TECH_DOC_TRIGGERS = (
    "چه پارامترهایی داره",
    "چه پارامترهایی میگیره",
    "پارامترهای",
    "قابلیت‌های جدید",
    "قابلیت های جدید",
    "تغییرات نسخه",
    "changelog",
    "documentation",
    "داکیومنت",
)

CLOCK_PATTERNS = (
    "ساعت چنده",
    "ساعت چند است",
    "ساعت چند",
    "چه ساعتیه",
    "چه ساعتی است",
    "چه ساعتی",
    "الان چه ساعتیه",
    "الان ساعت چنده",
    "ساعت الان",
    "تاریخ امروز",
    "امروز چه تاریخیه",
    "امروز چه روزیه",
    "امروز چندمه",
    "امروز چندم",
    "چه روزیه",
    "چه روزی است",
    "چه تاریخیه",
    "چه تاریخی است",
    "what time is it",
    "what is the time",
    "current time",
    "time in tehran",
)

CASUAL_CHAT_PREFIXES = (
    "سلام",
    "درود",
    "خوبی",
    "چطوری",
    "چخبر",
    "چه خبر",
    "جونم",
    "قربون",
    "فدات",
    "عشقم",
    "صبح بخیر",
    "شب بخیر",
)

_URL_DETECTION_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from text."""
    if not text:
        return []
    return _URL_DETECTION_RE.findall(text)


def has_url(text: str) -> bool:
    """Return True if text contains at least one web URL."""
    return bool(_URL_DETECTION_RE.search(text or ""))


def resolve_search_intent(text: str) -> tuple[SearchDecision, str]:
    """Determine whether search is required, suggested, or unnecessary.

    Returns (decision, reason).
    """
    raw = (text or "").strip()
    if not raw:
        return SearchDecision.NO_SEARCH, "empty_input"

    # Normalize Persian variations
    norm = normalize_persian_text(raw).casefold()

    # 1. URL present → must fetch page directly
    if has_url(raw):
        return SearchDecision.MUST_SEARCH, "url_supplied"

    # 2. Explicit user request → MUST_SEARCH (Hard Rule, no heuristics or clock can override)
    for trigger in EXPLICIT_SEARCH_TRIGGERS:
        if trigger in norm:
            return SearchDecision.MUST_SEARCH, f"explicit_user_request:{trigger}"

    # 3. Clock query → clock tool handles it directly, no search
    if any(clock in norm for clock in CLOCK_PATTERNS):
        return SearchDecision.NO_SEARCH, "clock_question"

    # 4. Time-sensitive / financial / breaking news → MUST_SEARCH
    for trigger in TIME_SENSITIVE_TRIGGERS:
        if trigger in norm:
            return SearchDecision.MUST_SEARCH, f"time_sensitive:{trigger}"

    # 5. Technical specs / documentation / release queries → SHOULD_SEARCH
    for trigger in TECH_DOC_TRIGGERS:
        if trigger in norm:
            return SearchDecision.SHOULD_SEARCH, f"technical_documentation:{trigger}"

    # 6. Casual greetings / short chat → NO_SEARCH
    compact = re.sub(r"\s+", " ", norm)
    if len(compact) <= 30 and any(compact.startswith(p) or compact == p for p in CASUAL_CHAT_PREFIXES):
        return SearchDecision.NO_SEARCH, "casual_conversation"

    return SearchDecision.NO_SEARCH, "stable_knowledge_or_chat"


def extract_clean_search_query(text: str) -> str:
    """Extract the real search keywords by stripping explicit conversational prefixes."""
    norm = normalize_persian_text(text or "").strip()
    # Strip reply context markers if present
    marker = "[درخواست فعلی]"
    if marker in norm:
        norm = norm.rsplit(marker, 1)[-1].strip()

    # Remove trigger phrases like 'اینو سرچ کن', 'یه سرچ بزن ببین', 'بررسی کن', etc.
    cleaned = re.sub(
        r"(?:اینو\s+)?(?:یه\s+)?(?:سرچ(?:ش)?|جستجو|جست‌وجو|گوگل)\s*(?:کن|بزن|کنش|کنید)?(?:\s+ببین|\s+ببینم)?",
        " ",
        norm,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"(?:تو\s+اینترنت\s+)(?:بگرد|نگاه کن|ببین)", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:بررسی\s+کن|پیدا\s+کن|از\s+سایت\s+پیدا\s+کن)", " ", cleaned, flags=re.IGNORECASE)
    # Remove leading/trailing punctuation and extra whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t,،:.-؟?!")
    return cleaned or norm
