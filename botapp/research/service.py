"""Unified ResearchService orchestrating web, x, and youtube search,
normalization, deduplication, ranking, and evidence generation.
"""
from __future__ import annotations

import asyncio
import logging
from time import monotonic

from .errors import normalize_error
from .evidence import format_research_evidence
from .models import ProviderStatus, ResearchItem, ResearchResult
from .providers.web import WebResearchProvider
from .providers.x import XResearchProvider
from .providers.youtube import YouTubeResearchProvider
from .ranking import deduplicate, rank_items

logger = logging.getLogger(__name__)

DEFAULT_TOTAL_TIMEOUT = 25.0


class ResearchService:
    def __init__(
        self,
        *,
        web: WebResearchProvider | None = None,
        x: XResearchProvider | None = None,
        youtube: YouTubeResearchProvider | None = None,
    ):
        self.web = web or WebResearchProvider()
        self.x = x or XResearchProvider()
        self.youtube = youtube or YouTubeResearchProvider()

    def _get_provider(self, name: str):
        if name == "web":
            return self.web
        if name in ("x", "twitter"):
            return self.x
        if name in ("youtube", "yt"):
            return self.youtube
        return None

    async def _query_provider(
        self, name: str, query: str, limit: int, **kwargs
    ) -> tuple[str, list[ResearchItem], ProviderStatus]:
        prov = self._get_provider(name)
        norm_name = "youtube" if name in ("youtube", "yt") else ("x" if name in ("x", "twitter") else name)
        t0 = monotonic()

        if not prov:
            return (
                norm_name,
                [],
                ProviderStatus(name=norm_name, status="capability_unavailable", error="unknown provider"),
            )

        if not prov.is_available():
            return (
                norm_name,
                [],
                ProviderStatus(name=norm_name, status="disabled", duration_ms=0.0),
            )

        try:
            items = await prov.search(query, limit=limit, **kwargs)
            duration = (monotonic() - t0) * 1000
            status = ProviderStatus(
                name=norm_name,
                status="ok",
                count=len(items),
                duration_ms=round(duration, 1),
            )
            return norm_name, items, status
        except Exception as exc:
            duration = (monotonic() - t0) * 1000
            err_code = normalize_error(exc)
            logger.warning("research provider %s failed for query %r: %s", norm_name, query[:30], type(exc).__name__)
            status = ProviderStatus(
                name=norm_name,
                status=err_code,
                count=0,
                duration_ms=round(duration, 1),
                error=type(exc).__name__,
            )
            return norm_name, [], status

    async def social_search(
        self,
        query: str,
        *,
        sources: list[str] | None = None,
        limit_per_source: int = 8,
        depth: str = "standard",
        time_range: str = "7d",
    ) -> ResearchResult:
        clean_q = (query or "").strip()
        if not clean_q:
            return ResearchResult(status="ok", items=[], providers={})

        active_sources = list(dict.fromkeys(s.lower() for s in (sources or ["web", "x", "youtube"])))
        limit = min(max(1, limit_per_source), 15)

        # Run providers concurrently
        tasks = [
            asyncio.create_task(self._query_provider(src, clean_q, limit))
            for src in active_sources
        ]

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=False),
                timeout=DEFAULT_TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("social_search total timeout query=%r", clean_q[:40])
            results = []

        all_items: list[ResearchItem] = []
        statuses: dict[str, ProviderStatus] = {}

        for norm_name, items, status in results:
            statuses[norm_name] = status
            all_items.extend(items)

        # Normalization + Deduplication + Ranking
        deduped = deduplicate(all_items)
        ranked = rank_items(deduped, query=clean_q)

        # Generate Evidence
        max_evidence_items = 12 if depth == "deep" else (4 if depth == "quick" else 8)
        evidence = format_research_evidence(ranked, max_items=max_evidence_items)

        # Check overall status
        ok_count = sum(1 for s in statuses.values() if s.status == "ok")
        overall_status = "ok" if (ok_count == len(statuses) and ok_count > 0) else "partial"

        return ResearchResult(
            status=overall_status,
            items=ranked,
            providers=statuses,
            evidence=evidence,
            truncated=len(deduped) > max_evidence_items,
        )


default_research_service = ResearchService()
