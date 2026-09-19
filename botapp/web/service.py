"""Unified WebService coordinating search, security, fetch, ranking, and evidence assembly."""
from __future__ import annotations

import asyncio
import logging
import os
from time import monotonic
from typing import Any

from .errors import BlockedURLError, SearchBudgetExceeded, SearchError, WebFetchError
from .evidence import EvidenceBuilder
from .fetcher import StaticFetcher
from .models import EvidenceChunk, SearchBudget, SearchDecision, SearchResult, SearchTrace, WebDocument
from .policy import (
    extract_clean_search_query,
    extract_urls,
    has_url,
    normalize_persian_text,
    resolve_search_intent,
)
from .providers.base import SearchProvider
from .providers.duckduckgo import DuckDuckGoProvider
from .ranking import SearchResultRanker

logger = logging.getLogger(__name__)


class WebService:
    """Production-grade Web Intelligence service for NoyaBot."""

    def __init__(self, provider: SearchProvider | None = None, fetcher: StaticFetcher | None = None):
        self.provider = provider or DuckDuckGoProvider()
        self.fetcher = fetcher or StaticFetcher()

    @staticmethod
    def is_enabled() -> bool:
        return os.getenv("NOYA_SEARCH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

    async def search(self, query: str, limit: int = 8, *, trace: SearchTrace | None = None) -> list[SearchResult]:
        """Perform web search with ranking and deduplication."""
        clean_q = extract_clean_search_query(query)
        if not clean_q:
            return []

        t0 = monotonic()
        if trace:
            trace.queries.append(clean_q)
            trace.provider = self.provider.name

        try:
            raw_results = await self.provider.search(clean_q, limit=limit)
            ranked = SearchResultRanker.rank(raw_results, clean_q)
            elapsed_ms = (monotonic() - t0) * 1000
            if trace:
                trace.result_count = len(ranked)
                trace.selected_results = [r.url for r in ranked[:3]]
                trace.latency_ms += elapsed_ms

            logger.info("web_search_finished query=%s count=%d latency=%.1fms", clean_q[:40], len(ranked), elapsed_ms)
            return ranked
        except Exception as exc:
            logger.warning("web_search_failed query=%s error=%s", clean_q[:40], exc)
            return []

    async def fetch_page(self, url: str, *, trace: SearchTrace | None = None) -> WebDocument:
        """Fetch and extract web document with SSRF protection."""
        t0 = monotonic()
        if trace:
            trace.fetched_urls.append(url)

        try:
            doc = await self.fetcher.fetch(url)
            elapsed_ms = (monotonic() - t0) * 1000
            if trace:
                trace.latency_ms += elapsed_ms
            logger.info("web_fetch_finished url=%s status=%d chars=%d latency=%.1fms", url[:60], doc.status_code, doc.char_count, elapsed_ms)
            return doc
        except (BlockedURLError, WebFetchError) as exc:
            if trace:
                trace.fetch_failures.append({"url": url, "error": str(exc)})
            logger.warning("web_fetch_blocked_or_failed url=%s error=%s", url[:60], exc)
            raise
        except Exception as exc:
            if trace:
                trace.fetch_failures.append({"url": url, "error": str(exc)})
            logger.exception("web_fetch_unexpected_error url=%s", url[:60])
            raise WebFetchError(f"خطای غیرمنتظره در دریافت صفحه: {exc}") from exc

    async def fetch_and_build_evidence(self, url: str, query: str = "") -> str:
        """Fetch a single URL and return clean evidence block with safety boundary."""
        doc = await self.fetch_page(url)
        chunks = EvidenceBuilder.select_best_chunks(doc, query=query, max_chunks=3)
        return EvidenceBuilder.format_evidence_block(chunks)

    async def multi_source_research(self, query: str, max_sources: int = 2) -> tuple[str, list[dict[str, str]]]:
        """Search, rank results, concurrently fetch top authoritative sources, and construct evidence."""
        results = await self.search(query, limit=5)
        if not results:
            return "", []

        # Filter top sources
        top_results = results[:max_sources]
        sem = asyncio.Semaphore(3)

        async def _safe_fetch(r: SearchResult) -> tuple[SearchResult, WebDocument | None]:
            async with sem:
                try:
                    doc = await self.fetch_page(r.url)
                    return r, doc
                except Exception:
                    return r, None

        tasks = [_safe_fetch(r) for r in top_results]
        fetched_pairs = await asyncio.gather(*tasks, return_exceptions=False)

        all_chunks: list[EvidenceChunk] = []
        sources: list[dict[str, str]] = []

        for r, doc in fetched_pairs:
            if doc and doc.text:
                chunks = EvidenceBuilder.select_best_chunks(doc, query=query, max_chunks=2)
                all_chunks.extend(chunks)
                sources.append({"title": doc.title or r.title, "url": doc.final_url or r.url})
            elif r.snippet:
                # Fallback to snippet if page couldn't be fetched
                all_chunks.append(
                    EvidenceChunk(
                        source_id=f"S{len(all_chunks)+1}",
                        title=r.title,
                        url=r.url,
                        text=r.snippet,
                    )
                )
                sources.append({"title": r.title, "url": r.url})

        evidence_text = EvidenceBuilder.format_evidence_block(all_chunks)
        return evidence_text, sources


# Global default web service singleton
default_web_service = WebService()
