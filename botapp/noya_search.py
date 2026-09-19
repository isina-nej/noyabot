"""Lightweight live web search for Noya. Fail-open; never required to answer."""

from __future__ import annotations

import logging
import os
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

_SEARCH_TIMEOUT = float(os.getenv("NOYA_SEARCH_TIMEOUT", "8"))
_MAX_RESULTS = 5
_MAX_BLOCK_CHARS = 1800

_EXPLICIT = (
    "سرچ کن",
    "سرچش کن",
    "سرچ بزن",
    "سرچش بزن",
    "جستجو کن",
    "جستجو بزن",
    "جست‌وجو",
    "گوگل کن",
    "تو گوگل",
    "بگرد ببین",
    "پیدا کن",
    "search",
    "google",
)
_BARE_SEARCH_RE = re.compile(
    r"^(?:اینو\s+)?(?:سرچ(?:ش)?|جستجو|جست‌وجو|گوگل)\s*(?:کن|بزن|کنش)?[؟?!.]*$",
    re.IGNORECASE,
)
_LIVE = (
    "اخبار",
    "خبر",
    "قیمت دلار",
    "قیمت طلا",
    "نرخ ارز",
    "نرخ دلار",
    "آب و هوا",
    "هواشناسی",
    "نتیجه بازی",
    "جدول لیگ",
    "امشب چی",
    "امروز چه خبر",
    "آخرین خبر",
    "کی برد",
    "برنده انتخابات",
    "قیمت بیت",
    "crypto",
    "bitcoin",
)
_SKIP_PREFIX = (
    "سلام",
    "خوبی",
    "چطوری",
    "چخبر",
    "چه خبر",
    "جونم",
    "قربون",
    "عشقم",
    "صبح بخیر",
    "شب بخیر",
)
_CLOCK_ONLY = (
    "ساعت چنده",
    "ساعت چند است",
    "تاریخ امروز",
    "امروز چندمه",
    "امروز چندم",
    "چه روزیه",
    "چه روزی است",
)


def search_enabled() -> bool:
    return os.getenv("NOYA_SEARCH_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _current_ask(text: str) -> str:
    raw = (text or "").strip()
    marker = "[درخواست فعلی]"
    if marker in raw:
        raw = raw.rsplit(marker, 1)[-1]
    return re.sub(r"\s+", " ", raw).strip()


def extract_search_query(text: str) -> str:
    """Prefer the live ask; if it's a bare «سرچ بزن», use the replied message."""
    ask = _current_ask(text)
    if ask and not _BARE_SEARCH_RE.match(ask):
        cleaned = re.sub(
            r"(?:اینو\s+)?(?:سرچ(?:ش)?|جستجو|جست‌وجو|گوگل)\s*(?:کن|بزن|کنش)?",
            " ",
            ask,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t,،:.-؟?!")
        return cleaned or ask
    match = re.search(
        r"\[پیام مورد اشاره[^\]]*\]\s*\n(.+?)(?:\n\n|\n\[|$)",
        text or "",
        re.DOTALL,
    )
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()[:240]
    return ask


def needs_web_search(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    ask = _current_ask(raw)
    haystack = f"{ask}\n{raw}".casefold()
    if any(token in ask.casefold() for token in _CLOCK_ONLY):
        return False
    compact = re.sub(r"\s+", " ", ask.casefold())
    if len(compact) <= 24 and any(compact.startswith(p) or compact == p for p in _SKIP_PREFIX):
        return False
    if any(token in haystack for token in _EXPLICIT):
        return True
    if any(token in haystack for token in _LIVE):
        return True
    return False


class _LiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_link = False
        self._href = ""
        self._buf: list[str] = []
        self.results: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        data = dict(attrs)
        href = data.get("href") or ""
        if "uddg=" in href or data.get("class") == "result-link":
            self._in_link = True
            self._href = href
            self._buf = []

    def handle_endtag(self, tag):
        if tag != "a" or not self._in_link:
            return
        self._in_link = False
        title = unescape("".join(self._buf)).strip()
        url = _decode_ddg_url(self._href)
        if title and url and not url.startswith("https://duckduckgo.com"):
            self.results.append((title, url))

    def handle_data(self, data):
        if self._in_link:
            self._buf.append(data)


def _decode_ddg_url(href: str) -> str:
    href = unescape(href or "")
    if "uddg=" in href:
        qs = parse_qs(urlparse(href).query)
        target = (qs.get("uddg") or [""])[0]
        return unquote(target)
    if href.startswith("http"):
        return href
    return ""


def _format_block(lines: list[str]) -> str:
    body = "\n".join(line for line in lines if line).strip()
    if not body:
        return ""
    if len(body) > _MAX_BLOCK_CHARS:
        body = body[: _MAX_BLOCK_CHARS - 20] + "\n…[truncated]"
    return f"[WEB]\n{body}\n[/WEB]"


async def web_search(query: str) -> str:
    q = re.sub(r"\s+", " ", (query or "").strip())
    if not q:
        return ""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fa,en;q=0.8",
    }
    timeout = httpx.Timeout(_SEARCH_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            lite = await _duckduckgo_lite(client, q)
            if lite:
                return lite
            return await _duckduckgo_instant(client, q)
    except (httpx.HTTPError, ValueError, OSError):
        logger.info("noya_search_failed query=%s", q[:80])
        return ""


async def _duckduckgo_instant(client: httpx.AsyncClient, query: str) -> str:
    response = await client.get(
        "https://api.duckduckgo.com/",
        params={
            "q": query,
            "format": "json",
            "no_html": "1",
            "no_redirect": "1",
            "skip_disambig": "1",
            "kl": "ir-fa",
        },
    )
    response.raise_for_status()
    data = response.json()
    lines: list[str] = []
    heading = (data.get("Heading") or "").strip()
    abstract = (data.get("AbstractText") or data.get("Abstract") or "").strip()
    source = (data.get("AbstractURL") or "").strip()
    if abstract:
        label = heading or "خلاصه"
        lines.append(f"• {label}: {abstract}")
        if source:
            lines.append(f"  {source}")
    for item in (data.get("RelatedTopics") or [])[:_MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        text = (item.get("Text") or "").strip()
        url = (item.get("FirstURL") or "").strip()
        if text:
            lines.append(f"• {text}")
            if url:
                lines.append(f"  {url}")
        if len(lines) >= _MAX_RESULTS * 2:
            break
    return _format_block(lines)


async def _duckduckgo_lite(client: httpx.AsyncClient, query: str) -> str:
    response = await client.get(
        "https://lite.duckduckgo.com/lite/",
        params={"q": query, "kl": "ir-fa"},
    )
    response.raise_for_status()
    parser = _LiteParser()
    parser.feed(response.text)
    lines: list[str] = []
    seen: set[str] = set()
    for title, url in parser.results:
        if url in seen:
            continue
        seen.add(url)
        lines.append(f"• {title}")
        lines.append(f"  {url}")
        if len(seen) >= _MAX_RESULTS:
            break
    return _format_block(lines)


async def maybe_web_search(question: str) -> str:
    if not search_enabled() or not needs_web_search(question):
        return ""
    query = extract_search_query(question)
    if not query:
        return ""
    block = await web_search(query)
    if block:
        logger.info("noya_search_hit qlen=%s query=%s", len(question or ""), query[:80])
    return block
