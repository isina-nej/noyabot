"""Web page content extractor and metadata builder."""
from __future__ import annotations

import re
from html import unescape
from typing import Any

from . import dates
from .cleaner import ContentCleaner
from .models import WebDocument

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_META_AUTHOR_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:author|article:author)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_META_DATE_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:article:published_time|pubdate|date|publish-date|'
    r'release_date|dc.date|DC.date.issued|sailthru.date)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
# JSON-LD structured data: "datePublished": "2025-03-11T08:00:00+00:00"
_JSONLD_DATE_RE = re.compile(
    r'"datePublished"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
_JSONLD_CREATED_RE = re.compile(
    r'"dateCreated"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
# <time datetime="2025-03-11"> visible semantic markup
_TIME_DATETIME_RE = re.compile(
    r'<time[^>]+datetime=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
# Last-resort fallback: the page's own "last updated" stamp.
_META_UPDATED_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:og:updated_time|article:modified_time|'
    r'last-modified|lastmod|revised)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)

_YEAR_RE = re.compile(r"(?:1[89]|20)\d{2}")


class ContentExtractor:
    """Extracts clean text and metadata into a WebDocument."""

    @staticmethod
    def extract(html: str, url: str, final_url: str = "", status_code: int = 200, content_type: str = "text/html") -> WebDocument:
        if not html:
            return WebDocument(url=url, final_url=final_url or url, status_code=status_code, content_type=content_type)

        title = ""
        description = ""
        author = ""
        published_at = ""

        # Metadata extraction
        m_title = _TITLE_RE.search(html)
        if m_title:
            title = unescape(m_title.group(1)).strip()
            title = re.sub(r"\s+", " ", title)

        m_desc = _META_DESC_RE.search(html)
        if m_desc:
            description = unescape(m_desc.group(1)).strip()

        m_auth = _META_AUTHOR_RE.search(html)
        if m_auth:
            author = unescape(m_auth.group(1)).strip()

        published_at = _extract_published_at(html)

        # Clean readable text
        clean_text = ContentCleaner.clean(html)

        # Detect language heuristic
        lang = "fa" if any("\u0600" <= c <= "\u06ff" for c in clean_text[:200]) else "en"

        return WebDocument(
            url=url,
            final_url=final_url or url,
            title=title,
            description=description,
            author=author,
            published_at=published_at,
            text=clean_text,
            language=lang,
            content_type=content_type,
            status_code=status_code,
            char_count=len(clean_text),
        )


def _normalize_date_value(raw: str) -> str:
    """Normalize a candidate timestamp to ``YYYY-MM-DD``, or ``""`` if unusable."""
    if not raw:
        return ""
    value = unescape(raw).strip().strip("\"'")
    if not value:
        return ""
    return dates.format_source_date(value)


def _extract_published_at(html: str) -> str:
    """Best-effort SOURCE PUBLICATION date for a page, never a fabricated one.

    Priority order (first usable candidate wins):

    1. ``<meta>`` publication tags (``article:published_time``, ``pubdate``,
       ``date`` …) — the historical behaviour;
    2. JSON-LD ``datePublished`` / ``dateCreated``;
    3. ``<time datetime="…">`` semantic markup;
    4. ``og:updated_time`` / ``article:modified_time`` as an explicit fallback,
       because a page with no publish stamp is better described by when it was
       last touched than by nothing at all.

    Values are normalized to ``YYYY-MM-DD`` when parseable; an unparseable
    ``<meta>`` value is kept verbatim (historical behaviour) rather than thrown
    away, while unparseable JSON-LD/``<time>`` candidates are skipped so a
    ``<time datetime="PT5M">`` duration can never masquerade as a date.
    """
    for regex in (_META_DATE_RE,):
        m = regex.search(html)
        if m:
            raw = unescape(m.group(1)).strip()
            if raw:
                normalized = _normalize_date_value(raw)
                return normalized or raw

    for regex in (_JSONLD_DATE_RE, _JSONLD_CREATED_RE):
        m = regex.search(html)
        if m:
            normalized = _normalize_date_value(m.group(1))
            if normalized:
                return normalized

    m = _TIME_DATETIME_RE.search(html)
    if m:
        raw = unescape(m.group(1)).strip()
        normalized = _normalize_date_value(raw)
        if normalized:
            return normalized
        if _YEAR_RE.search(raw):
            return raw

    m = _META_UPDATED_RE.search(html)
    if m:
        normalized = _normalize_date_value(m.group(1))
        if normalized:
            return normalized
    return ""
