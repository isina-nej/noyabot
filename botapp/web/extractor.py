"""Web page content extractor and metadata builder."""
from __future__ import annotations

import re
from html import unescape
from typing import Any

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
    r'<meta[^>]+(?:name|property)=["\'](?:article:published_time|pubdate|date)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)


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

        m_date = _META_DATE_RE.search(html)
        if m_date:
            published_at = unescape(m_date.group(1)).strip()

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
