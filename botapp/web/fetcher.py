"""Secure asynchronous web page fetcher with SSRF protection and redirect validation."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from .errors import BlockedURLError, WebFetchError
from .extractor import ContentExtractor
from .models import WebDocument
from .security import validate_url_security

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 12.0
DEFAULT_TOTAL_TIMEOUT = 18.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB


class StaticFetcher:
    """Production-hardened HTTP fetcher preventing SSRF, loops, and runaway payloads."""

    def __init__(
        self,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ):
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.total_timeout = total_timeout
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self._client: httpx.AsyncClient | None = None

    async def get_client(self) -> httpx.AsyncClient:
        """Get or initialize reusable HTTP client with connection pooling."""
        if self._client is None or self._client.is_closed:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fa,en;q=0.8",
            }
            timeout = httpx.Timeout(
                self.total_timeout,
                connect=self.connect_timeout,
                read=self.read_timeout,
            )
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                limits=limits,
                follow_redirects=False,  # Redirects manually validated for SSRF
            )
        return self._client

    async def close(self) -> None:
        """Gracefully close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str) -> WebDocument:
        """Fetch a web page, validate security, handle redirects, and extract content."""
        target_url = (url or "").strip().rstrip(".,!?;:،؛»\"')]")
        if not target_url.startswith(("http://", "https://")):
            target_url = f"https://{target_url}"

        # 1. Initial SSRF validation
        validate_url_security(target_url)

        client = await self.get_client()
        current_url = target_url
        redirect_count = 0
        visited = {current_url}

        while True:
            try:
                # Stream response to enforce size limits before reading entire payload
                async with client.stream("GET", current_url) as resp:
                    status = resp.status_code

                    # Handle Redirects manually with SSRF re-check
                    if status in (301, 302, 303, 307, 308):
                        redirect_count += 1
                        if redirect_count > self.max_redirects:
                            raise WebFetchError(f"تعداد ریدایرکت‌ها از سقف مجاز ({self.max_redirects}) فراتر رفت.")
                        location = resp.headers.get("location")
                        if not location:
                            raise WebFetchError("هدایت‌کننده ریدایرکت فاقد هدر Location است.")
                        next_url = urljoin(current_url, location)
                        if next_url in visited:
                            raise WebFetchError("حلقه نامتناهی در ریدایرکت‌های صفحه کشف شد.")
                        visited.add(next_url)
                        # CRITICAL: Re-validate security for redirected target URL
                        validate_url_security(next_url)
                        current_url = next_url
                        continue

                    if status >= 400:
                        raise WebFetchError(f"خطای سرور HTTP {status} در دریافت {current_url}")

                    content_type = resp.headers.get("content-type", "").lower()
                    if "text" not in content_type and "html" not in content_type and "json" not in content_type:
                        return WebDocument(
                            url=target_url,
                            final_url=current_url,
                            status_code=status,
                            content_type=content_type,
                            text=f"[محتوای غیر متنی پشتیبانی نمی‌شود: {content_type}]",
                        )

                    # Read body with size budget
                    body_chunks: list[bytes] = []
                    bytes_read = 0
                    async for chunk in resp.aiter_bytes():
                        bytes_read += len(chunk)
                        if bytes_read > self.max_bytes:
                            raise WebFetchError(f"حجم صفحه فراتر از سقف مجاز ({self.max_bytes // (1024*1024)}MB) است.")
                        body_chunks.append(chunk)

                    raw_bytes = b"".join(body_chunks)

                    # Determine encoding
                    encoding = resp.encoding or "utf-8"
                    try:
                        html_text = raw_bytes.decode(encoding, errors="replace")
                    except Exception:
                        html_text = raw_bytes.decode("utf-8", errors="replace")

                    # Extract structured document
                    doc = ContentExtractor.extract(
                        html=html_text,
                        url=target_url,
                        final_url=current_url,
                        status_code=status,
                        content_type=content_type,
                    )
                    return doc

            except (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                raise WebFetchError(f"زمان دریافت صفحه به پایان رسید: {exc}") from exc
            except httpx.RequestError as exc:
                raise WebFetchError(f"خطا در برقراری ارتباط شبکه: {exc}") from exc
