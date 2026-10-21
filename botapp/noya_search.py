"""Backward-compatible wrapper for Noya web search backed by the botapp.web layer."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from botapp.web import (
    SearchDecision,
    default_web_service,
    extract_clean_search_query,
    normalize_persian_text,
    resolve_search_intent,
)
from botapp.web.dates import format_source_date
from botapp.web.policy import freshness_intent
from botapp.web.ranking import recency_summary

logger = logging.getLogger(__name__)

TEHRAN_TZ = ZoneInfo("Asia/Tehran")

_MAX_BLOCK_CHARS = 2400
_MAX_BLOCK_RESULTS = 5
_MAX_SNIPPET_CHARS = 240
_MAX_TITLE_CHARS = 200

_NOTE_LINE = (
    "note=date is the SOURCE PUBLICATION date, not the event date; "
    "date=unknown means the source published no date"
)

_VALID_FRESHNESS = ("latest", "recent", "general")
_BARE_SEARCH_RE = re.compile(
    r"^(?:اینو\s+)?(?:سرچ(?:ش)?|جستجو|جست‌وجو|گوگل)\s*(?:کن|بزن|کنش)?[؟?!.]*$",
    re.IGNORECASE,
)


def search_enabled() -> bool:
    """Check if web search is enabled globally."""
    return default_web_service.is_enabled()


def extract_search_query(text: str) -> str:
    """Extract search query, using referenced message context if current ask is bare."""
    raw = (text or "").strip()
    marker = "[درخواست فعلی]"
    ask = raw.rsplit(marker, 1)[-1].strip() if marker in raw else raw

    # If ask is just "سرچ کن" / "سرچ بزن", extract from replied message block
    if ask and _BARE_SEARCH_RE.match(ask):
        match = re.search(
            r"\[پیام مورد اشاره[^\]]*\]\s*\n(.+?)(?:\n\n|\n\[|$)",
            raw,
            re.DOTALL,
        )
        if match:
            ref_text = re.sub(r"\s+", " ", match.group(1)).strip()
            # If ref text has [NOW], strip it or use content
            ref_clean = re.sub(r"\[NOW\][^\n]*", "", ref_text).strip()
            return ref_clean[:240] or ref_text[:240]

    return extract_clean_search_query(ask)


def needs_web_search(text: str) -> bool:
    """Determine if a text prompt requires live web search."""
    if not text or not search_enabled():
        return False

    raw = text.strip()
    # Check referenced message payload if present
    marker = "[درخواست فعلی]"
    if marker in raw:
        ask = raw.rsplit(marker, 1)[-1].strip()
        if _BARE_SEARCH_RE.match(ask):
            return True

    decision, _ = resolve_search_intent(raw)
    return decision in (SearchDecision.MUST_SEARCH, SearchDecision.SHOULD_SEARCH)


def _host_of(url: str) -> str:
    """Host of a result URL for the ``source=`` field (``www.`` stripped)."""
    try:
        host = urlparse(url or "").netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _single_line(text: str, limit: int) -> str:
    """Collapse to one line and clamp — never re-writes numbers or units."""
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", str(text)).strip()
    return collapsed[:limit].rstrip()


def _format_block(results, *, freshness: str = "general", now: datetime | None = None) -> str:
    """Render ranked results as the canonical ``[WEB]`` block.

    Exact contract (the system prompt reads this verbatim)::

        [WEB]
        searched_at=<ISO-8601 +03:30> freshness=<latest|recent|general> results=<n> dated=<k> unknown=<u>
        recency_warning=<oldest known age in days + "d">      # only when stale
        note=date is the SOURCE PUBLICATION date, not the event date; date=unknown means the source published no date
        • <title> | date=<YYYY-MM-DD|unknown> | source=<host>
          <snippet, single line, max 240 chars>
          <url>
        [/WEB]

    ``date=`` is always the SOURCE PUBLICATION date and is ``unknown`` when the
    provider exposed none — it is never guessed. No results means an empty
    string: we do not fabricate a source.
    """
    if not results:
        return ""

    if freshness not in _VALID_FRESHNESS:
        freshness = "general"

    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    summary = recency_summary(results, freshness, now=reference)
    selected = list(results[:_MAX_BLOCK_RESULTS])

    lines: list[str] = [
        f"searched_at={reference.astimezone(TEHRAN_TZ).replace(microsecond=0).isoformat()}"
        f" freshness={freshness}"
        f" results={len(selected)}"
        f" dated={summary['known']}"
        f" unknown={summary['unknown']}"
    ]

    # Surface staleness instead of pretending an old source is current.
    if freshness in ("latest", "recent") and summary.get("stale"):
        oldest = summary.get("oldest_days")
        if oldest is not None:
            lines.append(f"recency_warning={oldest}d")

    lines.append(_NOTE_LINE)

    for r in selected:
        title = _single_line(getattr(r, "title", ""), _MAX_TITLE_CHARS) or "(بدون عنوان)"
        published = format_source_date(getattr(r, "published_at", "")) or "unknown"
        host = _host_of(getattr(r, "url", "")) or getattr(r, "source", "") or "unknown"
        lines.append(f"• {title} | date={published} | source={host}")
        snippet = _single_line(getattr(r, "snippet", ""), _MAX_SNIPPET_CHARS)
        if snippet:
            lines.append(f"  {snippet}")
        url = getattr(r, "url", "")
        if url:
            lines.append(f"  {url}")

    body = "\n".join(lines).strip()
    if not body:
        return ""
    if len(body) > _MAX_BLOCK_CHARS:
        body = body[: _MAX_BLOCK_CHARS - 20] + "\n…[truncated]"
    return f"[WEB]\n{body}\n[/WEB]"


async def web_search(query: str, *, freshness: str | None = None) -> str:
    """Execute search and return the canonical [WEB] block format."""
    clean_q = extract_clean_search_query(query)
    if not clean_q:
        return ""
    intent = freshness if freshness in _VALID_FRESHNESS else freshness_intent(query)
    results = await default_web_service.search(clean_q, limit=_MAX_BLOCK_RESULTS, freshness=intent)
    return _format_block(results, freshness=intent)


async def maybe_web_search(question: str) -> str:
    """Conditionally execute web search based on search policy."""
    if not search_enabled() or not needs_web_search(question):
        return ""
    query = extract_search_query(question)
    if not query:
        return ""
    # Freshness comes from the ORIGINAL question: "جدیدترین قیمت دلار" keeps
    # its "latest" intent even after the search-query cleanup.
    block = await web_search(query, freshness=freshness_intent(question))
    if block:
        logger.info("noya_search_hit query=%s", query[:60])
    return block
