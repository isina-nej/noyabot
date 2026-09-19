"""HTML cleaner and semantic DOM parser for extracting readable content."""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

# Tags to completely remove with all their inner content
STRIP_CONTENT_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "iframe",
    "form",
    "select",
    "button",
    "dialog",
    "menu",
    "template",
    "nav",
    "footer",
    "header",
    "aside",
}

# Substrings in class or id attributes that signal noise elements
NOISE_SELECTORS = (
    "cookie",
    "banner",
    "modal",
    "popup",
    "newsletter",
    "subscribe",
    "sidebar",
    "widget",
    "advert",
    "social-share",
    "share-btn",
    "menu",
    "navigation",
    "breadcrumb",
    "footer",
    "header",
)


class ContentCleaner:
    """Cleans HTML removing noise, scripts, and navigation while preserving articles, tables, and lists."""

    @staticmethod
    def clean(html: str) -> str:
        if not html:
            return ""

        # Try BeautifulSoup if available for pristine DOM-level tree pruning
        try:
            import bs4  # type: ignore
            return ContentCleaner._clean_with_bs4(html)
        except Exception:
            return ContentCleaner._clean_with_regex(html)

    @staticmethod
    def _clean_with_bs4(html: str) -> str:
        from bs4 import BeautifulSoup, Comment  # type: ignore

        soup = BeautifulSoup(html, "html.parser")

        # 1. Remove comments
        for comment in soup.find_all(text=lambda text: isinstance(text, Comment)):
            comment.extract()

        # 2. Decompose noise tags
        for tag in soup.find_all(list(STRIP_CONTENT_TAGS)):
            tag.decompose()

        # 3. Decompose noise classes/ids
        for el in soup.find_all(True):
            attrs = el.attrs
            classes = " ".join(attrs.get("class", [])) if isinstance(attrs.get("class"), list) else str(attrs.get("class", ""))
            el_id = str(attrs.get("id", ""))
            attr_text = f"{classes} {el_id}".lower()
            if any(noise in attr_text for noise in NOISE_SELECTORS):
                # Don't delete if it's the main container (like 'article' or 'main')
                if el.name not in ("body", "html", "main", "article"):
                    el.decompose()

        # 4. Format structured markdown
        lines: list[str] = []

        # Headings
        for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            level = int(h.name[1])
            text = h.get_text().strip()
            if text:
                h.replace_with(f"\n\n{'#' * level} {text}\n\n")

        # Code blocks
        for pre in soup.find_all("pre"):
            code = pre.get_text().strip()
            if code:
                pre.replace_with(f"\n\n```\n{code}\n```\n\n")

        # Tables
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                table.replace_with("\n\n" + "\n".join(rows) + "\n\n")

        # Lists
        for li in soup.find_all("li"):
            text = li.get_text(strip=True)
            if text:
                li.replace_with(f"\n• {text}")

        # Paragraphs
        for p in soup.find_all("p"):
            text = p.get_text(strip=True)
            if text:
                p.replace_with(f"\n\n{text}\n\n")

        text = soup.get_text()
        text = unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return text

    @staticmethod
    def _clean_with_regex(html: str) -> str:
        # 1. Strip content noise tags (script, style, nav, footer, header, aside, etc.)
        pattern = r"<(?:%s)[^>]*>.*?</(?:%s)>" % ("|".join(STRIP_CONTENT_TAGS), "|".join(STRIP_CONTENT_TAGS))
        cleaned = re.sub(pattern, " ", html, flags=re.DOTALL | re.IGNORECASE)

        # 2. Strip elements with noise classes/ids (cookie banners, sidebars, modals, ads)
        noise_pattern = r"<(\w+)[^>]+(?:class|id)=[\"'][^\"']*(?:%s)[^\"']*[\"'][^>]*>.*?</\1>" % "|".join(NOISE_SELECTORS)
        cleaned = re.sub(noise_pattern, " ", cleaned, flags=re.DOTALL | re.IGNORECASE)

        # 3. Format tables: convert <tr> with <td>/<th> to piped rows
        def _fmt_table(m: re.Match) -> str:
            table_html = m.group(0)
            rows = []
            for tr_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.DOTALL | re.IGNORECASE):
                cells = [
                    re.sub(r"<[^>]+>", " ", c.group(1)).strip()
                    for c in re.finditer(r"<(?:th|td)[^>]*>(.*?)</(?:th|td)>", tr_match.group(1), flags=re.DOTALL | re.IGNORECASE)
                ]
                if cells:
                    rows.append(" | ".join(cells))
            return "\n\n" + "\n".join(rows) + "\n\n" if rows else " "

        cleaned = re.sub(r"<table[^>]*>.*?</table>", _fmt_table, cleaned, flags=re.DOTALL | re.IGNORECASE)

        # 4. Format lists <li>
        cleaned = re.sub(r"<li[^>]*>(.*?)</li>", r"\n• \1", cleaned, flags=re.DOTALL | re.IGNORECASE)

        # 5. Format headings
        for i in range(1, 7):
            cleaned = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>", rf"\n\n{'#'*i} \1\n\n", cleaned, flags=re.DOTALL | re.IGNORECASE)

        # 6. Strip all remaining HTML tags
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = unescape(cleaned)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n\s*\n+", "\n\n", cleaned).strip()
        return cleaned
