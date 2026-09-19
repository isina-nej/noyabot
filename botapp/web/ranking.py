"""Search result ranking, domain authority scoring, and URL canonicalization."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import SearchResult

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
    """Ranks search results using domain authority, position, and query keyword overlap."""

    @staticmethod
    def rank(results: list[SearchResult], query: str = "") -> list[SearchResult]:
        if not results:
            return []

        query_tokens = set(re.findall(r"\w+", query.casefold()))
        seen_urls: set[str] = set()
        deduped: list[SearchResult] = []

        for res in results:
            canon = canonicalize_url(res.url)
            if canon in seen_urls:
                continue
            seen_urls.add(canon)
            res.url = canon
            deduped.append(res)

        for idx, res in enumerate(deduped):
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

        # Sort descending by score
        deduped.sort(key=lambda r: r.score, reverse=True)
        for idx, res in enumerate(deduped):
            res.rank = idx + 1

        return deduped
