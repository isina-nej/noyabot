"""DuckDuckGo search provider implementing SearchProvider protocol."""
from __future__ import annotations

import asyncio
import logging
import random
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from ..errors import SearchProviderError
from ..models import SearchResult
from .base import SearchProvider

logger = logging.getLogger("noya.web.duckduckgo")


class _DDGLiteParser(HTMLParser):
    """Parses DuckDuckGo Lite HTML search results from POST response."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self.in_link = False
        self.in_snippet = False
        self.current_href = ""
        self.buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        cls = attr_dict.get("class") or ""
        href = attr_dict.get("href") or ""

        if tag == "a" and ("result-link" in cls or "uddg=" in href):
            self.in_link = True
            self.current_href = href
            self.buf = []
        elif tag == "td" and "result-snippet" in cls:
            self.in_snippet = True
            self.buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.in_link:
            self.in_link = False
            title = unescape("".join(self.buf)).strip()
            url = self.current_href
            if "uddg=" in url:
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                uddg_vals = qs.get("uddg", [])
                if uddg_vals:
                    url = unquote(uddg_vals[0])
            if title and url and not url.startswith("https://duckduckgo.com"):
                self.results.append({"title": title, "url": url, "snippet": ""})
        elif tag == "td" and self.in_snippet:
            self.in_snippet = False
            snippet = unescape("".join(self.buf)).strip()
            if self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = snippet

    def handle_data(self, data: str) -> None:
        if self.in_link or self.in_snippet:
            self.buf.append(data)


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo provider using Lite HTML POST with instant API and Wikipedia fallbacks."""

    def __init__(self, timeout: float = 8.0, max_retries: int = 2) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def get_client(self) -> httpx.AsyncClient:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if (
            self._client is None
            or self._client.is_closed
            or (self._loop is not None and current_loop is not None and self._loop != current_loop)
        ):
            self._client = None
            self._loop = current_loop
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=4.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []

        client = await self.get_client()

        # Try Lite POST
        for attempt in range(self.max_retries):
            try:
                results = await self._search_lite(client, query, limit)
                if results:
                    return results
                break
            except Exception as exc:
                if attempt == self.max_retries - 1:
                    logger.warning("DDG Lite failed after retries: %s", exc)
                else:
                    await asyncio.sleep(0.5 + random.uniform(0.1, 0.4))

        # Fallback 1: DuckDuckGo Instant Answer API (suitable for conceptual queries)
        is_live_query = bool(re.search(
            r"(?:قیمت|نرخ|دلار|سکه|طلا|ارز|بیت\s*کوین|bitcoin|crypto|release|news|اخبار|جدیدترین|آخرین|امروز|الان|وضعیت|status|ورژن|نسخه|live|current)",
            query,
            re.IGNORECASE,
        ))

        try:
            results = await self._search_instant_api(client, query, limit)
            if results:
                return results
        except Exception as exc:
            logger.warning("DDG instant API failed: %s", exc)

        # Fallback 2: Wikipedia OpenSearch API (ONLY for general/encyclopedic queries, NEVER for live news/prices/releases!)
        if not is_live_query:
            try:
                results = await self._search_wikipedia_api(client, query, limit)
                if results:
                    return results
            except Exception as exc:
                logger.warning("Wikipedia API fallback failed: %s", exc)

        return []

    async def _search_lite(self, client: httpx.AsyncClient, query: str, limit: int) -> list[SearchResult]:
        resp = await client.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
        )
        if resp.status_code != 200:
            return []

        parser = _DDGLiteParser()
        parser.feed(resp.text)

        results: list[SearchResult] = []
        for i, item in enumerate(parser.results[:limit]):
            results.append(
                SearchResult(
                    title=item["title"],
                    url=item["url"],
                    snippet=item["snippet"],
                    source="duckduckgo",
                    rank=i + 1,
                )
            )
        return results

    async def _search_instant_api(self, client: httpx.AsyncClient, query: str, limit: int) -> list[SearchResult]:
        resp = await client.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "no_redirect": "1"},
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        results: list[SearchResult] = []
        rank = 1

        heading = data.get("Heading")
        abstract = data.get("Abstract")
        abstract_url = data.get("AbstractURL")
        if heading and (abstract or abstract_url):
            results.append(
                SearchResult(
                    title=heading,
                    url=abstract_url or "https://duckduckgo.com",
                    snippet=abstract or heading,
                    source="duckduckgo-instant",
                    rank=rank,
                )
            )
            rank += 1

        for topic in data.get("RelatedTopics", []):
            if len(results) >= limit:
                break
            if isinstance(topic, dict) and "Text" in topic and "FirstURL" in topic:
                results.append(
                    SearchResult(
                        title=topic.get("Text", "").split(" - ")[0],
                        url=topic["FirstURL"],
                        snippet=topic.get("Text", ""),
                        source="duckduckgo-related",
                        rank=rank,
                    )
                )
                rank += 1

        return results

    async def _search_wikipedia_api(self, client: httpx.AsyncClient, query: str, limit: int) -> list[SearchResult]:
        is_persian = any("\u0600" <= c <= "\u06ff" for c in query)
        endpoint = "https://fa.wikipedia.org/w/api.php" if is_persian else "https://en.wikipedia.org/w/api.php"
        resp = await client.get(
            endpoint,
            params={"action": "opensearch", "search": query, "limit": limit, "format": "json"},
            headers={"User-Agent": "NoyaBot/1.0 (admin@noyabot.org)"},
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        if not isinstance(data, list) or len(data) < 4:
            return []

        titles = data[1]
        descriptions = data[2]
        urls = data[3]

        results: list[SearchResult] = []
        for i in range(min(len(titles), len(urls), limit)):
            results.append(
                SearchResult(
                    title=titles[i],
                    url=urls[i],
                    snippet=descriptions[i] if i < len(descriptions) and descriptions[i] else titles[i],
                    source="wikipedia",
                    rank=i + 1,
                )
            )
        return results
