"""Ranking and deduplication for cross-provider research results."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from .models import ResearchItem

# Domain authority modifiers for social / media platforms
PLATFORM_AUTHORITY = {
    "reuters.com": 2.5,
    "apnews.com": 2.5,
    "bbc.com": 2.3,
    "bbc.co.uk": 2.3,
    "nytimes.com": 2.2,
    "theguardian.com": 2.0,
    "techcrunch.com": 2.0,
    "arstechnica.com": 2.0,
    "youtube.com": 1.8,
    "x.com": 1.5,
    "twitter.com": 1.5,
    "github.com": 2.0,
    "stackoverflow.com": 1.8,
    "wikipedia.org": 1.9,
}

# Source type credibility weight
SOURCE_TYPE_WEIGHT = {
    "article": 1.5,
    "page": 1.3,
    "video": 1.2,
    "channel": 1.1,
    "post": 1.0,
    "comment": 0.6,
}


def _recency_score(published_at: str | None, max_days: int = 30) -> float:
    if not published_at:
        return 0.3
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        if age < 0:
            return 1.0
        days = age / 86400
        if days > max_days:
            return 0.1
        return max(0.1, 1.0 - (days / max_days) * 0.8)
    except Exception:
        return 0.3


def _engagement_score(engagement: dict | None) -> float:
    if not engagement:
        return 0.0
    total = sum(
        int(engagement.get(k, 0) or 0)
        for k in ("likes", "views", "replies", "reposts", "comments")
    )
    if total <= 0:
        return 0.0
    import math
    return min(2.0, math.log10(total + 1) * 0.5)


def rank_items(items: list[ResearchItem], query: str = "") -> list[ResearchItem]:
    if not items:
        return []

    query_tokens = set(re.findall(r"\w+", (query or "").casefold()))

    for item in items:
        score = 1.0

        # Source type weight
        score *= SOURCE_TYPE_WEIGHT.get(item.source_type, 1.0)

        # Domain authority
        try:
            host = urlparse(item.url).netloc.lower()
            for domain, boost in PLATFORM_AUTHORITY.items():
                if host == domain or host.endswith("." + domain):
                    score *= boost
                    break
        except Exception:
            pass

        # Recency
        score += _recency_score(item.published_at) * 2.0

        # Engagement (capped — viral != true)
        score += _engagement_score(item.engagement)

        # Query relevance
        if query_tokens:
            text_to_match = f"{item.title or ''} {item.text or ''}".casefold()
            matches = sum(1 for t in query_tokens if t in text_to_match)
            if query_tokens:
                score += (matches / len(query_tokens)) * 3.0

        item.score = round(score, 2)

    items.sort(key=lambda i: i.score, reverse=True)
    return items


def deduplicate(items: list[ResearchItem]) -> list[ResearchItem]:
    seen: set[str] = set()
    out: list[ResearchItem] = []
    for item in items:
        key = item.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
