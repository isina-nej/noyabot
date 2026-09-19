"""Backward-compatible wrapper for Noya web search backed by the botapp.web layer."""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from botapp.web import (
    SearchDecision,
    default_web_service,
    extract_clean_search_query,
    normalize_persian_text,
    resolve_search_intent,
)

logger = logging.getLogger(__name__)

_MAX_BLOCK_CHARS = 2400
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


def _format_block(results) -> str:
    if not results:
        return ""
    lines: list[str] = []
    for r in results[:5]:
        title = getattr(r, "title", "")
        url = getattr(r, "url", "")
        snippet = getattr(r, "snippet", "")
        if title:
            lines.append(f"• {title}")
        if snippet:
            lines.append(f"  {snippet}")
        if url:
            lines.append(f"  {url}")

    body = "\n".join(lines).strip()
    if not body:
        return ""
    if len(body) > _MAX_BLOCK_CHARS:
        body = body[: _MAX_BLOCK_CHARS - 20] + "\n…[truncated]"
    return f"[WEB]\n{body}\n[/WEB]"


async def web_search(query: str) -> str:
    """Execute search and return legacy [WEB] block format."""
    clean_q = extract_clean_search_query(query)
    if not clean_q:
        return ""
    results = await default_web_service.search(clean_q, limit=5)
    return _format_block(results)


async def maybe_web_search(question: str) -> str:
    """Conditionally execute web search based on search policy."""
    if not search_enabled() or not needs_web_search(question):
        return ""
    query = extract_search_query(question)
    if not query:
        return ""
    block = await web_search(query)
    if block:
        logger.info("noya_search_hit query=%s", query[:60])
    return block
