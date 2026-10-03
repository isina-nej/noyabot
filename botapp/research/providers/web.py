"""Web research provider wrapping botapp.web.default_web_service."""
from __future__ import annotations

import logging

from botapp.web import default_web_service
from ..models import ResearchItem

logger = logging.getLogger(__name__)


class WebResearchProvider:
    name = "web"

    def is_available(self) -> bool:
        return default_web_service.is_enabled()

    async def search(self, query: str, *, limit: int = 10, **kwargs) -> list[ResearchItem]:
        clean_q = (query or "").strip()
        if not clean_q:
            return []
        try:
            results = await default_web_service.search(clean_q, limit=limit)
            out: list[ResearchItem] = []
            for r in results:
                out.append(
                    ResearchItem(
                        platform="web",
                        source_type="article",
                        source_id=None,
                        author=None,
                        title=r.title,
                        text=r.snippet,
                        published_at=None,
                        url=r.url,
                        engagement=None,
                        metadata={"source": r.source, "rank": r.rank},
                        score=r.score,
                    )
                )
            return out
        except Exception as exc:
            logger.warning("web research search failed query=%s: %s", clean_q[:40], exc)
            return []
