"""Search result ranking, domain authority scoring, and URL canonicalization."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .dates import age_days
from .models import SearchResult

# How many days of age still counts as "fresh" for each freshness intent.
FRESHNESS_WINDOWS: dict[str, int] = {
    "latest": 7,
    "recent": 30,
}

# Beyond this age a source is unambiguously stale for a freshness request.
_STALE_AFTER_DAYS = 180

# The known-old penalty is mild and clamped: 1% of the relevance score lost per
# day past the window, never dropping below 50% of the original score.
_PENALTY_SPAN_DAYS = 100
_MIN_AGE_PENALTY = 0.5

# High-trust authoritative technical domains
AUTHORITATIVE_DOMAINS = {
    "docs.python.org": 2.5,
    "python.org": 2.2,
    "docs.djangoproject.com": 2.5,
    "djangoproject.com": 2.2,
    "ai.google.dev": 2.5,
    "cloud.google.com": 2.2,
    "github.com": 2.0,
    "gitlab.com": 1.8,
    "pypi.org": 2.2,
    "tgju.org": 2.5,  # Gold/currency standard in Iran
    "bonbast.com": 2.5,
    "developer.mozilla.org": 2.5,
    "wikipedia.org": 1.9,
    "fa.wikipedia.org": 2.0,
    "en.wikipedia.org": 2.0,
    "stackoverflow.com": 1.8,
}

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "gclsrc",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}


def canonicalize_url(url: str) -> str:
    """Normalize a URL to prevent duplicate visits (strip tracking params, trailing slashes, fragments)."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # Filter tracking query parameters
        filtered_query = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS
        ]
        new_query = urlencode(filtered_query)
        # Strip trailing slash from path (unless path is just '/')
        path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
        canonical = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            parsed.params,
            new_query,
            "",  # Strip fragment #
        ))
        return canonical
    except Exception:
        return url.strip().rstrip("/")


class SearchResultRanker:
    """Ranks search results using domain authority, position, and query keyword overlap.

    Ranking is layered, and recency is a *conditional* layer on top:

    * ``freshness="general"`` — the historical behaviour, byte for byte:
      relevance/authority only, dated and undated sources treated equally.
    * ``freshness="latest"`` / ``"recent"`` — a hard tier ordering on top of
      the relevance score so a 3-year-old page can never be presented as
      "the latest" merely because its domain is authoritative:

          tier 0  known date inside the window      (fresh)
          tier 1  no known date                     (unknown)
          tier 2  known date outside the window     (older)
          tier 3  known date older than 180 days    (stale)

      Inside a tier we keep the pre-existing relevance score (with a mild,
      age-scaled penalty for out-of-window results) and finally fall back to
      the original provider position, so the order is fully deterministic.

    Only the pre-existing relevance score depends on ``now``; nothing is ever
    dropped because it is old.
    """

    @staticmethod
    def _base_score(results: list[SearchResult], query: str) -> None:
        """Compute the pre-existing relevance/authority score in place."""
        query_tokens = set(re.findall(r"\w+", query.casefold()))
        for idx, res in enumerate(results):
            score = 10.0 / (idx + 1)  # Initial position rank

            # Domain authority boost
            try:
                host = urlparse(res.url).netloc.lower()
                for auth_domain, boost in AUTHORITATIVE_DOMAINS.items():
                    if host == auth_domain or host.endswith("." + auth_domain):
                        score *= boost
                        break
                # Docs subdomain boost
                if host.startswith("docs.") or host.startswith("developer."):
                    score *= 1.3
            except Exception:
                pass

            # Query keyword relevance
            text_to_match = f"{res.title} {res.snippet}".casefold()
            match_count = sum(1 for token in query_tokens if token in text_to_match)
            if query_tokens:
                score += (match_count / len(query_tokens)) * 5.0

            res.score = round(score, 2)

    @staticmethod
    def rank(
        results: list[SearchResult],
        query: str = "",
        *,
        freshness: str = "general",
        now: datetime | None = None,
    ) -> list[SearchResult]:
        if not results:
            return []

        # Original provider order — the final, fully deterministic tiebreaker.
        positions = {id(res): idx for idx, res in enumerate(results)}

        seen_urls: set[str] = set()
        deduped: list[SearchResult] = []

        for res in results:
            canon = canonicalize_url(res.url)
            if canon in seen_urls:
                continue
            seen_urls.add(canon)
            res.url = canon
            deduped.append(res)

        SearchResultRanker._base_score(deduped, query)

        reference = now if now is not None else datetime.now(timezone.utc)
        # Age is measured for every dated result, whether or not the caller
        # asked for recency ordering, so downstream formatting can report it.
        for res in deduped:
            res.age_days = age_days(res.published_at, now=reference) if res.published_at else None

        window = FRESHNESS_WINDOWS.get(freshness)
        if window is None:
            # general: no recency boost and no recency penalty. This branch is
            # deliberately identical to the historical ranking.
            deduped.sort(key=lambda r: (-r.score, positions.get(id(r), 0)))
        else:

            def sort_key(res: SearchResult) -> tuple[int, float, int]:
                age = res.age_days
                if age is None:
                    # unknown never outranks a known-fresh source
                    return 1, -res.score, positions.get(id(res), 0)
                if age <= window:
                    return 0, -res.score, positions.get(id(res), 0)
                tier = 3 if age > _STALE_AFTER_DAYS else 2
                # Mild penalty for known-old results, proportional to how far
                # past the window they are (clamped so it stays "mild").
                over = age - window
                factor = max(_MIN_AGE_PENALTY, 1.0 - (over / float(_PENALTY_SPAN_DAYS)))
                return tier, -(res.score * factor), positions.get(id(res), 0)

            deduped.sort(key=sort_key)

        for idx, res in enumerate(deduped):
            res.rank = idx + 1

        return deduped


def recency_summary(
    results: list[SearchResult],
    freshness: str = "general",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarise how well a result set answers a freshness request.

    Returning ``stale`` lets the answer qualify old sources out loud instead of
    silently presenting them as "the latest":

    * ``stale is True``  — the newest KNOWN date is older than the window for
      this freshness intent (7 days for ``latest``, 30 for ``recent``);
    * ``stale is None``  — no result carried a date at all, so freshness can
      neither be confirmed nor denied;
    * ``stale is False`` — at least one known date is inside the window.
    """
    reference = now if now is not None else datetime.now(timezone.utc)
    known_ages: list[int] = []
    unknown = 0
    for res in results or []:
        age = res.age_days
        if age is None and getattr(res, "published_at", ""):
            age = age_days(res.published_at, now=reference)
        if age is None:
            unknown += 1
        else:
            known_ages.append(age)

    summary: dict[str, Any] = {
        "known": len(known_ages),
        "unknown": unknown,
        "newest_days": min(known_ages) if known_ages else None,
        "oldest_days": max(known_ages) if known_ages else None,
        "stale": None,
    }
    if not known_ages:
        # Undated sources: we cannot claim freshness, and we must not invent it.
        summary["stale"] = None
        return summary

    window = FRESHNESS_WINDOWS.get(freshness)
    if window is None:
        summary["stale"] = False
    else:
        summary["stale"] = summary["newest_days"] > window
    return summary
