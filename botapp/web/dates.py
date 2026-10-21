"""Publication-date parsing helpers for the Noya web intelligence layer.

The single job of this module is to answer, honestly, "when was this source
published?" — never "when did the event happen?" and never a guess.

Design rules:

* every function returns ``None`` / ``""`` instead of raising;
* ambiguous or unknown input (Jalali month names, nonsense strings, empty
  snippets) returns ``None`` rather than a fabricated date;
* everything that parses is returned as a timezone-aware UTC ``datetime``;
* relative expressions ("2 days ago", "دیروز") only resolve when ``now`` is
  supplied, and only when they actually look like a date statement.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Final

UTC: Final = timezone.utc

# ── digit / character normalization ──

_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_CHAR_MAP = {
    # Arabic -> Persian letter variants
    "ي": "ی",
    "ى": "ی",
    "ك": "ک",
    "ة": "ه",
    "ۀ": "ه",
    "ﻻ": "لا",
    "ـ": "",  # tatweel / kashida
    # Arabic-Indic separators that masquerade as digits or spaces
    "\u200b": "",
    "\u200e": "",
    "\u200f": "",
    "\u200d": "",
    "\u200c": " ",  # ZWNJ becomes a space so "۲‌روز‌پیش" matches
    "\u00a0": " ",
    "\u0640": "",
}

# Gregorian month names. Both Latin and the Persian transliterations of the
# GREGORIAN calendar are unambiguous; Jalali month names (فروردین … اسفند)
# are deliberately absent — we refuse to guess a calendar conversion.
_MONTHS: dict[str, int] = {
    # Latin
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
    # Persian transliterations of Gregorian months
    "ژانویه": 1, "فوریه": 2, "مارس": 3,
    "آوریل": 4, "اوریل": 4, "اپریل": 4,
    "مه": 5, "می": 5,
    "ژوئن": 6, "جون": 6,
    "ژوئیه": 7, "جولای": 7,
    "اوت": 8, "آگوست": 8, "اگوست": 8,
    "سپتامبر": 9, "اکتبر": 10,
    "نوامبر": 11, "دسامبر": 12,
}

_MONTH_ALTERNATION = "|".join(
    sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True)
)

# ── compiled patterns ──

# 2025-03-11 / 2025/03/11 / 2025.03.11 with optional time and offset.
_ISO_RE = re.compile(
    r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})"
    r"(?:[T ]+(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:\.(?P<micro>\d{1,6}))?)?"
    r"\s*(?P<tz>Z|UTC|GMT|[+-]\d{2}:?\d{2})?)?",
    re.IGNORECASE,
)

# 11 March 2025 / 11 Mar 2025 / ۱۱ مارس ۲۰۲۵
_DAY_MONTH_YEAR_RE = re.compile(
    rf"(?P<day>\d{{1,2}})\s*(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_ALTERNATION})"
    rf"\.?,?\s+(?P<year>\d{{4}})",
    re.IGNORECASE,
)

# Mar 11, 2025 / March 11 2025 / مارس ۱۱ ۲۰۲۵
_MONTH_DAY_YEAR_RE = re.compile(
    rf"(?P<month>{_MONTH_ALTERNATION})\.?,?\s+(?P<day>\d{{1,2}})"
    rf"(?:st|nd|rd|th)?,?\s+(?P<year>\d{{4}})",
    re.IGNORECASE,
)

# "2 days ago" / "3 hours ago" — the digit is what makes this trustworthy
# enough to accept anywhere inside a snippet.
_UNIT_SECONDS: dict[str, int] = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
    "month": 2592000, "months": 2592000,  # 30 days
    "year": 31536000, "years": 31536000,  # 365 days
    "ثانیه": 1, "دقیقه": 60, "ساعت": 3600, "روز": 86400,
    "هفته": 604800, "ماه": 2592000, "سال": 31536000,
}

_UNIT_ALTERNATION = "|".join(
    sorted((re.escape(u) for u in _UNIT_SECONDS), key=len, reverse=True)
)

# "2 days ago" / "۲ روز پیش" / "3 hours ago"
_RELATIVE_QUANTITY_RE = re.compile(
    rf"(?P<qty>\d{{1,3}})\s*(?P<unit>{_UNIT_ALTERNATION})\s*"
    rf"(?:ago|before|قبل|پیش|گذشته)",
    re.IGNORECASE,
)

# Bare relative phrases — no digit, so they are only trusted when they OPEN
# the text (as a date stamp does: "دیروز", "yesterday · 10:00"). A phrase
# buried in prose ("قیمت دلار امروز ۱۰۴,۵۰۰ تومان") is a currency statement,
# not a publication date, so it is rejected rather than guessed.
_BARE_RELATIVE: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\b(?:just\s+now|همین\s*الان)\b", re.IGNORECASE), 0),
    (re.compile(r"\b(?:today|امروز)\b", re.IGNORECASE), 0),
    (re.compile(r"\b(?:yesterday|دیروز)\b", re.IGNORECASE), 86400),
    (re.compile(r"\b(?:last\s+week|هفته\s*(?:پیش|گذشته)|هفتهٔ\s*گذشته)\b", re.IGNORECASE), 604800),
    (re.compile(r"\b(?:last\s+month|ماه\s*(?:پیش|گذشته))\b", re.IGNORECASE), 2592000),
)


def _normalize(text: str) -> str:
    """Fold digits, Persian/Arabic letter variants, ZWNJ and whitespace."""
    if not text:
        return ""
    out = text.translate(_DIGIT_MAP)
    for src, dst in _CHAR_MAP.items():
        if src in out:
            out = out.replace(src, dst)
    out = re.sub(r"\s+", " ", out)
    return out.strip()


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _build(year: int, month: int, day: int,
           hour: int = 0, minute: int = 0, second: int = 0,
           micro: int = 0, tz: str | None = None) -> datetime | None:
    """Assemble a validated, UTC-normalized datetime or None."""
    if not (1 <= month <= 12):
        return None
    if not (1 <= day <= 31):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    if year < 1990 or year > 2100:  # sanity window for web sources
        return None
    offset = UTC
    if tz and tz.upper() not in {"Z", "UTC", "GMT"}:
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        try:
            hours = int(digits[:2])
            minutes = int(digits[2:4]) if len(digits) >= 4 else 0
            offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
        except (ValueError, IndexError):
            offset = UTC
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=offset).astimezone(UTC)
    except ValueError:
        # e.g. 31 April — reject instead of rolling over
        return None


def _parse_absolute(text: str) -> datetime | None:
    """Absolute Gregorian date in any of the supported notations."""
    m = _ISO_RE.search(text)
    if m:
        return _build(
            int(m.group("year")), int(m.group("month")), int(m.group("day")),
            int(m.group("hour") or 0), int(m.group("minute") or 0),
            int(m.group("second") or 0),
            int((m.group("micro") or "0").ljust(6, "0")),
            m.group("tz"),
        )

    m = _DAY_MONTH_YEAR_RE.search(text)
    if m:
        month = _MONTHS.get(m.group("month").casefold())
        if month:
            return _build(int(m.group("year")), month, int(m.group("day")))

    m = _MONTH_DAY_YEAR_RE.search(text)
    if m:
        month = _MONTHS.get(m.group("month").casefold())
        if month:
            return _build(int(m.group("year")), month, int(m.group("day")))

    return None


def _parse_relative(text: str, now: datetime) -> datetime | None:
    """Relative expression resolved against ``now`` (UTC)."""
    m = _RELATIVE_QUANTITY_RE.search(text)
    if m:
        unit = m.group("unit").casefold()
        seconds = _UNIT_SECONDS.get(unit)
        if seconds is None:
            return None
        try:
            qty = int(m.group("qty"))
        except ValueError:
            return None
        if qty < 0 or qty > 999:
            return None
        return now - timedelta(seconds=seconds * qty)

    # Bare phrases: only when they open the text, never buried in prose.
    for pattern, delta in _BARE_RELATIVE:
        m = pattern.match(text)
        if not m:
            continue
        return now - timedelta(seconds=delta)
    return None


def parse_iso_date(value: str) -> datetime | None:
    """Parse an ISO-8601-ish date/datetime string into a UTC datetime.

    Accepts ``2025-03-11``, ``2025-03-11T08:00:00+00:00``, ``2025/03/11`` and
    friends. Returns ``None`` for anything else — it never guesses.
    """
    if not value or not isinstance(value, str):
        return None
    text = _normalize(value)
    if not text:
        return None
    m = _ISO_RE.fullmatch(text)
    if not m:
        # Allow a bare ISO stamp wrapped in stray punctuation.
        m = _ISO_RE.search(text)
        if not m:
            return None
    return _build(
        int(m.group("year")), int(m.group("month")), int(m.group("day")),
        int(m.group("hour") or 0), int(m.group("minute") or 0),
        int(m.group("second") or 0),
        int((m.group("micro") or "0").ljust(6, "0")),
        m.group("tz"),
    )


def parse_published_date(text: str, *, now: datetime | None = None) -> datetime | None:
    """Best-effort publication date for a snippet, meta value or article line.

    Absolute Gregorian dates are always attempted. Relative expressions only
    resolve when ``now`` is provided, because they are meaningless without a
    reference point. Unknown, ambiguous and Jalali-calendar inputs return
    ``None`` — callers must render those as ``unknown`` rather than inventing
    a date.
    """
    if not text or not isinstance(text, str):
        return None
    normalized = _normalize(text)
    if not normalized:
        return None

    if now is not None:
        relative = _parse_relative(normalized, _to_utc(now))
        if relative is not None:
            return relative

    return _parse_absolute(normalized)


def _coerce_published(published, *, now: datetime | None = None) -> datetime | None:
    """Accept datetime | date | str | None and return a UTC datetime."""
    if published is None:
        return None
    if isinstance(published, datetime):
        return _to_utc(published)
    if isinstance(published, str):
        stripped = published.strip()
        if not stripped:
            return None
        return parse_iso_date(stripped) or parse_published_date(stripped, now=now)
    # datetime.date and anything date-like
    try:
        return _to_utc(datetime(published.year, published.month, published.day))
    except (AttributeError, TypeError, ValueError):
        return None


def age_days(published, *, now: datetime | None = None) -> int | None:
    """Whole days between ``published`` and ``now`` (default: right now).

    Future dates clamp to 0 rather than going negative; unknown input yields
    ``None`` so callers can distinguish "no date" from "today".
    """
    parsed = _coerce_published(published, now=now)
    if parsed is None:
        return None
    reference = _to_utc(now) if now is not None else datetime.now(UTC)
    delta = reference - parsed
    days = int(delta.total_seconds() // 86400)
    return max(0, days)


def format_source_date(published) -> str:
    """Render a publication date as ``YYYY-MM-DD``, or ``""`` when unknown."""
    parsed = _coerce_published(published)
    if parsed is None:
        return ""
    return parsed.astimezone(UTC).strftime("%Y-%m-%d")


__all__ = [
    "age_days",
    "format_source_date",
    "parse_iso_date",
    "parse_published_date",
]
